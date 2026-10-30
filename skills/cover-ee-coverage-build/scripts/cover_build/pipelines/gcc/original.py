"""Plan and execute the original GCC, CMake, or Make build."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any


from cover_build.core.errors import WorkflowWaiting
from cover_build.planning import build_plan
from cover_build.common.processes import _command_environment
from cover_build.common.processes import _run
from cover_build.common.processes import _without_output
from cover_build.common import envelopes
from cover_build.common import native_logs
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.common import project_files
from cover_build.core import errors


def _reset_original_cmake_build_directory(job: dict[str, Any], build_directory: Path) -> dict[str, Any]:
    """Remove only generated CMake state before the native configure phase."""
    project_root = pathutil.canonical(job["projectRoot"], False)
    job_directory = pathutil.canonical(
        job.get("jobDirectory") or Path(job["directories"]["temporary"]).parent,
        False,
    )
    build_directory = pathutil.canonical(build_directory, False)
    if pathutil.same_path(build_directory, project_root) or pathutil.path_within(build_directory, job_directory):
        raise errors.ExecutorError(
            "INVALID_ORIGINAL_CMAKE_BUILD_DIRECTORY",
            "The original CMake build directory cannot be the project root or a job-local directory",
            [str(build_directory)],
        )
    build_directory.mkdir(parents=True, exist_ok=True)
    generated_entries = (
        "CMakeCache.txt",
        "CMakeFiles",
        "cmake_install.cmake",
        "CTestTestfile.cmake",
        "DartConfiguration.tcl",
        "Makefile",
        "build.ninja",
        "rules.ninja",
        ".ninja_deps",
        ".ninja_log",
        "install_manifest.txt",
    )
    removed: list[str] = []
    for name in generated_entries:
        target = build_directory / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
            removed.append(str(target))
        elif target.exists() or target.is_symlink():
            target.unlink()
            removed.append(str(target))
    return {
        "status": "RESET",
        "purpose": "ORIGINAL_NATIVE_CONFIGURE",
        "buildDirectory": str(build_directory),
        "removedGeneratedEntries": removed,
    }

def _cmake_original_commands(
    job: dict[str, Any],
    selection: dict[str, Any],
    build_tool: str,
    configure_arguments: list[str],
    targets: list[str],
    parallel: int,
    timeout: int,
) -> tuple[list[tuple[str, dict[str, Any]]], Path | None]:
    source = Path(selection["sourceDirectory"])
    # A previous COVER build may have left wrapper paths in CMakeCache.txt.
    # Always emit a native configure phase; execute_gcc() resets generated
    # CMake state immediately before running it so this configure cannot reuse
    # the coverage compiler selection.
    original_build_directory = Path(selection.get("originalBuildDirectory") or selection["buildDirectory"])
    arguments = ["-S", str(source), "-B", str(original_build_directory)]
    if selection.get("configuration"):
        arguments.append(f"-DCMAKE_BUILD_TYPE={selection['configuration']}")
    arguments.extend(["-DCMAKE_VERBOSE_MAKEFILE:BOOL=ON", *configure_arguments])
    if selection.get("generator"):
        arguments.extend(["-G", selection["generator"]])
    configure = processes.command_spec(Path(build_tool), arguments, source, timeout)
    common = ["--build", str(original_build_directory), "--verbose"]
    if selection.get("configuration"):
        common.extend(["--config", selection["configuration"]])
    build_arguments = [*common, "--clean-first", "--parallel", str(parallel)]
    if targets:
        build_arguments.extend(["--target", *targets])
    rebuild = processes.command_spec(Path(build_tool), build_arguments, source, timeout)
    return [("configure", configure), ("rebuild", rebuild)], original_build_directory / "CMakeCache.txt"

def _native_driver_commands(
    selection: dict[str, Any],
    build_tool: str,
    targets: list[str],
    parallel: int,
    clean_target: str | None,
    timeout: int,
) -> tuple[list[tuple[str, dict[str, Any]]], Path | None]:
    driver = selection["driver"]
    working_directory = Path(selection["buildDirectory"])
    entry = Path(selection["entry"])
    if driver == "MAKE":
        file_arguments = ["-f", str(entry)]
        if clean_target is None:
            return [
                ("commands", processes.command_spec(Path(build_tool), [*file_arguments, "-n", "-B", *targets], working_directory, timeout)),
                ("rebuild", processes.command_spec(Path(build_tool), [*file_arguments, "-B", f"-j{parallel}", *targets], working_directory, timeout)),
            ], None
        return [
            ("commands", processes.command_spec(Path(build_tool), [*file_arguments, "-n", "-B", *targets], working_directory, timeout)),
            ("clean", processes.command_spec(Path(build_tool), [*file_arguments, clean_target], working_directory, timeout)),
            ("build", processes.command_spec(Path(build_tool), [*file_arguments, f"-j{parallel}", *targets], working_directory, timeout)),
        ], None
    raise errors.ExecutorError("UNSUPPORTED_BUILD_SYSTEM", f"Unsupported original build driver: {driver}")

def create_gcc_original_plan(
    job: dict[str, Any],
    selection: dict[str, Any],
    build_tool: str,
    configure_arguments: list[str],
    targets: list[str],
    parallel: int,
    clean_target: str | None,
    evidence: list[str],
    timeout: int = 3600,
    evidence_source: str = "SCRIPT",
) -> dict[str, Any]:
    """Materialize the exact native commands before executing any customer build."""
    if selection["driver"] == "CMAKE":
        command_items, _ = _cmake_original_commands(
            job,
            selection,
            build_tool,
            configure_arguments,
            targets,
            parallel,
            timeout,
        )
        original_build_directory = (
            selection.get("originalBuildDirectory")
            or selection["buildDirectory"]
        )
    else:
        command_items, _ = _native_driver_commands(
            selection,
            build_tool,
            targets,
            parallel,
            clean_target,
            timeout,
        )
        original_build_directory = selection["buildDirectory"]
    coverage_injection: dict[str, Any] = {
        "strategy": {
            "CMAKE": "CMAKE_COMPILER_OVERRIDE",
            "MAKE": "MAKE_VARIABLE_OVERRIDE",
        }[selection["driver"]]
    }
    if selection["driver"] == "CMAKE":
        coverage_injection["configureArguments"] = list(configure_arguments)
    if selection["driver"] == "MAKE":
        coverage_injection["cleanTarget"] = clean_target
        coverage_injection["forceRebuild"] = clean_target is None
    return build_plan.create_original_build_plan(
        job,
        selection["driver"],
        build_tool,
        selection["entry"],
        dict(command_items),
        original_build_directory,
        list(selection["requiredRoles"]),
        evidence,
        evidence_source=evidence_source,
        generator=selection.get("generator"),
        configuration=selection.get("configuration"),
        targets=targets,
        coverage_injection=coverage_injection,
    )

def _resolve_command_tool(
    token: str,
    environment: dict[str, str],
    command_directory: Path | None = None,
) -> Path | None:
    value = token.strip().strip('"').strip("'")
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or any(separator in value for separator in ("/", "\\")):
        if not candidate.is_absolute():
            if command_directory is None:
                return None
            candidate = command_directory / candidate
        try:
            resolved = pathutil.canonical(candidate)
        except (errors.ExecutorError, OSError):
            return None
        return resolved if resolved.is_file() else None
    path_value = next((item for key, item in environment.items() if key.casefold() == "path"), None)
    found = shutil.which(value, path=path_value)
    return pathutil.canonical(found) if found else None

def _gcc_name_role(name: str) -> str | None:
    stem = name.casefold()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    if re.search(r"(?:^|-)g\+\+(?:-\d+(?:\.\d+)*)?$", stem):
        return "cxx_compiler"
    if re.search(r"(?:^|-)gcc(?:-\d+(?:\.\d+)*)?$", stem):
        return "c_compiler"
    if re.search(r"(?:^|-)ar(?:-\d+(?:\.\d+)*)?$", stem):
        return "ar"
    if re.search(r"(?:^|-)ld(?:\.(?:bfd|gold|lld)|-\d+(?:\.\d+)*)?$", stem):
        return "linker"
    return None

def _native_tool_roles(path: Path, line: str, tokens: list[str], required_roles: list[str]) -> tuple[str | None, list[str]]:
    gcc_role = _gcc_name_role(path.name)
    if gcc_role is not None:
        return "GCC", [gcc_role]
    name = path.name.casefold()
    if name in {"link.exe", "lld-link.exe"}:
        return "MSVC", ["linker"]
    if name in {"lib.exe", "llvm-lib.exe"}:
        return "MSVC", ["ar"]
    if name not in {"cl.exe", "clang-cl.exe"}:
        return None, []
    if not any(token.casefold() in {"-c", "/c"} for token in tokens):
        return "MSVC", []
    source_roles: set[str] = set()
    for token in tokens:
        suffix = Path(token.strip().strip('"').strip("'")).suffix.casefold()
        if suffix == ".c":
            source_roles.add("c_compiler")
        elif suffix in {".cc", ".cp", ".cpp", ".cxx", ".c++"}:
            source_roles.add("cxx_compiler")
    if not source_roles:
        source_roles.update(role for role in required_roles if role in {"c_compiler", "cxx_compiler"})
    return "MSVC", sorted(source_roles)


def _gcc_target_prefix(paths: dict[str, Path]) -> str | None:
    """Derive a GCC target prefix from resolved paths without persisting them."""
    prefixes: set[str] = set()
    suffixes = {"c_compiler": "gcc", "cxx_compiler": "g++"}
    for role, suffix in suffixes.items():
        path = paths.get(role)
        if path is None:
            continue
        name = re.sub(r"-(?:\d+(?:\.\d+)*)$", "", path.stem)
        if name.casefold().endswith(suffix):
            prefixes.add(name[: -len(suffix)].casefold())
    return next(iter(prefixes)) if len(prefixes) == 1 else None

def extract_gcc_toolchain(
    actual_text: str | Path,
    planned_text: str | Path | None,
    required_roles: list[str],
    environment: dict[str, str] | None = None,
    selected_tools: dict[str, str] | None = None,
    working_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Extract executable paths from CMake/Make native command listings.

    Actual verbose build output has priority.  Make command listings are
    used only for roles hidden by silent recipes.
    """
    environment = dict(environment or os.environ)
    selectors = {role: pathutil.canonical(path) for role, path in (selected_tools or {}).items()}
    tools: dict[str, Path] = {}
    invoked_tools: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    link_drivers: list[Path] = []
    link_driver_invoked_paths: list[str] = []
    observed_candidates: dict[str, list[Path]] = {}

    def record_tool(
        role: str,
        path: Path,
        line: str,
        provenance: str,
        invoked_path: str,
        response_expansion: dict[str, Any],
    ) -> None:
        item = {"path": invoked_path, "provenance": provenance, "line": line[:1000]}
        if response_expansion["responseFiles"]:
            item["responseFiles"] = response_expansion["responseFiles"]
        if response_expansion["unresolvedResponseFiles"]:
            item["unresolvedResponseFiles"] = response_expansion["unresolvedResponseFiles"]
        existing = tools.get(role)
        role_candidates = observed_candidates.setdefault(role, [])
        if not any(pathutil.same_path(path, candidate) for candidate in role_candidates):
            role_candidates.append(path)
        if existing is not None and not pathutil.same_path(existing, path):
            selected = selectors.get(role)
            if selected is None:
                raise WorkflowWaiting(
                    "ORIGINAL_TOOLCHAIN_AMBIGUOUS",
                    f"The original build used more than one {role}; the model must select the target tool",
                    {
                        "targetToolPaths": [],
                        "originalBuildCommandSource": ["MODEL_INFERRED"],
                    },
                    decision_owner="MODEL",
                    purpose="BUILD_ENVIRONMENT",
                    evidence=[
                        {
                            "role": role,
                            "candidates": sorted({str(existing), str(path)}, key=str.casefold),
                            "line": line[:1000],
                        }
                    ],
                )
            if pathutil.same_path(selected, path):
                tools[role] = path
                invoked_tools[role] = invoked_path
                evidence[role] = item
            elif not pathutil.same_path(selected, existing):
                raise errors.ExecutorError(
                    "TARGET_TOOL_NOT_OBSERVED",
                    f"Selected target tool for {role} was not one of the observed candidates",
                    [str(selected), str(existing), str(path)],
                )
        if existing is None:
            tools[role] = path
            invoked_tools[role] = invoked_path
            evidence[role] = item

    def inspect(text: str | Path, provenance: str) -> None:
        default_directory = pathutil.canonical(working_directory, False) if working_directory else None
        command_directory = default_directory
        for raw_line in processes.iter_output_lines(text):
            line = raw_line.strip()
            if not line:
                continue
            line = re.sub(r"^\[\d+/\d+\]\s+", "", line)
            directory_match = re.match(
                r"^cd\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))\s+&&\s+",
                line,
            )
            if directory_match:
                directory_value = next(value for value in directory_match.groups() if value is not None)
                directory_candidate = Path(directory_value)
                if not directory_candidate.is_absolute() and default_directory is not None:
                    directory_candidate = default_directory / directory_candidate
                try:
                    command_directory = pathutil.canonical(directory_candidate)
                except (errors.ExecutorError, OSError):
                    command_directory = default_directory
            analysis = native_logs.analyze_native_command_text(line)
            response_expansion = native_logs.expand_response_file_tokens(
                analysis["tokens"],
                command_directory,
            )
            expanded_tokens = response_expansion["tokens"]
            for token in analysis["executables"]:
                path = _resolve_command_tool(token, environment, command_directory)
                if path is None:
                    continue
                # Keep the invoked alias, but resolve bare names in the actual
                # build environment before passing them to Agent mappings.
                invoked = Path(token)
                if not invoked.is_absolute() and not invoked.root:
                    if any(separator in token for separator in ("/", "\\")):
                        invoked = (command_directory or Path.cwd()) / invoked
                    else:
                        search_path = next((v for k, v in environment.items() if k.casefold() == "path"), None)
                        invoked = Path(shutil.which(token, path=search_path) or token)
                    token = str(pathutil.absolute_literal(invoked))
                # PATHEXT may supply .EXE; store Windows executable suffixes as .exe.
                if os.name == "nt" and token.casefold().endswith(".exe"):
                    token = token[:-4] + ".exe"
                selected_roles = [
                    role
                    for role, selected in selectors.items()
                    if pathutil.same_path(path, selected)
                ]
                detected_family, detected_roles = _native_tool_roles(path, line, expanded_tokens, required_roles)
                if selected_roles:
                    # A typed cross-toolchain plan can identify customer tools
                    # whose executable names do not follow normal GCC naming.
                    family = detected_family or "GCC"
                    roles = list(dict.fromkeys([*selected_roles, *detected_roles]))
                else:
                    family, roles = detected_family, detected_roles
                if family is None:
                    continue
                has_compile_flag = any(value.casefold() in {"-c", "/c"} for value in expanded_tokens)
                has_output_flag = any(
                    value == "-o" or value.casefold().startswith("/out:")
                    for value in expanded_tokens
                )
                if selected_roles:
                    compiler_roles = [role for role in roles if role in {"c_compiler", "cxx_compiler"}]
                    non_compiler_roles = [role for role in roles if role not in {"c_compiler", "cxx_compiler"}]
                    if has_compile_flag and compiler_roles:
                        source_roles: set[str] = set()
                        for argument in expanded_tokens:
                            suffix = Path(argument.strip().strip('"').strip("'")).suffix.casefold()
                            if suffix == ".c":
                                source_roles.add("c_compiler")
                            elif suffix in {".cc", ".cp", ".cpp", ".cxx", ".c++"}:
                                source_roles.add("cxx_compiler")
                        applicable = [role for role in compiler_roles if not source_roles or role in source_roles]
                        for role in applicable:
                            record_tool(role, path, line, provenance, token, response_expansion)
                        continue
                    if not has_compile_flag:
                        for role in non_compiler_roles:
                            record_tool(role, path, line, provenance, token, response_expansion)
                        if compiler_roles and has_output_flag:
                            if not any(pathutil.same_path(path, current) for current in link_drivers):
                                link_drivers.append(path)
                                link_driver_invoked_paths.append(token)
                            evidence.setdefault(
                                "linker",
                                {
                                    "path": token,
                                    "provenance": provenance,
                                    "invocationKind": "COMPILER_DRIVER",
                                    "line": line[:1000],
                                    "responseFiles": response_expansion["responseFiles"],
                                },
                            )
                        continue
                if family == "GCC" and roles[0] in {"c_compiler", "cxx_compiler"} and not has_compile_flag:
                    if has_output_flag:
                        if not any(pathutil.same_path(path, current) for current in link_drivers):
                            link_drivers.append(path)
                            link_driver_invoked_paths.append(token)
                        evidence.setdefault(
                            "linker",
                            {
                                "path": token,
                                "provenance": provenance,
                                "invocationKind": "COMPILER_DRIVER",
                                "line": line[:1000],
                                "responseFiles": response_expansion["responseFiles"],
                            },
                        )
                    continue
                for role in roles:
                    record_tool(role, path, line, provenance, token, response_expansion)

    inspect(actual_text, "ORIGINAL_BUILD_VERBOSE_OUTPUT")
    if planned_text:
        inspect(planned_text, "NATIVE_COMMAND_LISTING_FALLBACK")
    # A GCC-family compiler driver may perform the final link without a
    # standalone ld invocation appearing in the verbose build. Promote that
    # observed driver before required-role and target-tool selection checks.
    if link_drivers and "linker" not in tools:
        selected_linker = selectors.get("linker")
        selected_index = next(
            (
                index
                for index, candidate in enumerate(link_drivers)
                if selected_linker is not None and pathutil.same_path(selected_linker, candidate)
            ),
            0,
        )
        link_driver = link_drivers[selected_index]
        tools["linker"] = link_driver
        invoked_tools["linker"] = link_driver_invoked_paths[selected_index]
        linker_candidates = observed_candidates.setdefault("linker", [])
        for candidate in link_drivers:
            if not any(pathutil.same_path(candidate, current) for current in linker_candidates):
                linker_candidates.append(candidate)
    for role, selected in selectors.items():
        candidates = observed_candidates.get(role, [])
        if not any(pathutil.same_path(selected, candidate) for candidate in candidates):
            raise errors.ExecutorError(
                "TARGET_TOOL_NOT_OBSERVED",
                f"Selected target tool for {role} was not observed in the original build evidence",
                [str(selected), *[str(candidate) for candidate in candidates]],
            )
    compiler_roles = {"c_compiler", "cxx_compiler"}
    observed_compiler_roles = compiler_roles.intersection(tools)
    missing = [role for role in required_roles if role not in tools and role not in compiler_roles]
    if not observed_compiler_roles and compiler_roles.intersection(required_roles):
        missing.extend(sorted(compiler_roles.intersection(required_roles)))
    if missing:
        raise WorkflowWaiting(
            "ORIGINAL_TOOLCHAIN_UNRESOLVED",
            "The original build succeeded but its required C/C++ tool paths could not be extracted",
            {"originalToolPath": missing},
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
        )
    reject_cover_wrappers(tools)
    compiler_families = {
        (
            "GCC"
            if role in selectors and pathutil.same_path(path, selectors[role])
            else _native_tool_roles(path, "", [], [role])[0]
        )
        for role, path in tools.items()
        if role in {"c_compiler", "cxx_compiler"}
    }
    compiler_families.discard(None)
    if len(compiler_families) != 1:
        raise WorkflowWaiting(
            "ORIGINAL_TOOLCHAIN_FAMILY_AMBIGUOUS",
            "The original build does not resolve to one compiler family",
            {"toolchainFamily": sorted(compiler_families)},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
        )
    result = {
        "family": next(iter(compiler_families)),
        # The toolchain contract records the executable token that the
        # original verbose build actually invoked. Resolved paths are used
        # only above for family validation and target-prefix calculation.
        "tools": dict(invoked_tools),
        "evidence": evidence,
        "linkDrivers": link_driver_invoked_paths,
        "unusedRoles": [
            role
            for role in ("c_compiler", "cxx_compiler", "ar", "linker")
            if role not in tools and not (role == "linker" and link_drivers)
        ],
        "requestedRolesNotObserved": sorted(set(required_roles) - set(tools)),
    }
    if result["family"] == "GCC":
        prefix = _gcc_target_prefix(tools)
        if prefix is not None:
            result["gccTargetPrefix"] = prefix
    return result

def extract_gcc_artifacts(text: str | Path, build_directory: Path) -> list[dict[str, Any]]:
    suffixes = {".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".elf", ".axf", ".out", ".bin", ".hex"}
    excluded_suffixes = {".o", ".obj", ".d", ".dep", ".gcno", ".gcda"}
    candidates: list[tuple[str, str, Path]] = []
    command_directory = build_directory
    for raw_line in processes.iter_output_lines(text):
        line = re.sub(r"^\s*\[\d+/\d+\]\s+", "", raw_line.strip())
        if not line:
            continue
        # CMake's verbose Makefile output commonly emits the working
        # directory on a separate line before the actual linker command:
        #   cd <dir> && cmake -E cmake_link_script ...
        # Relative outputs (for example ../../host-tools/tool) are relative
        # to that directory, not to the top-level build directory.
        cd_match = re.match(r"^cd\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))\s+&&\s+", line)
        if cd_match:
            directory_candidate = Path(next(value for value in cd_match.groups() if value))
            if not directory_candidate.is_absolute():
                directory_candidate = build_directory / directory_candidate
            command_directory = directory_candidate
        analysis = native_logs.analyze_native_command_text(line)
        tokens = native_logs.expand_response_file_tokens(
            analysis["tokens"],
            command_directory,
        )["tokens"]
        if any(token.casefold() in {"-c", "/c"} for token in tokens):
            continue
        for index, token in enumerate(tokens):
            if token == "-o" and index + 1 < len(tokens):
                candidates.append((tokens[index + 1], line, command_directory))
            elif token.casefold().startswith("/out:"):
                candidates.append((token.split(":", 1)[1].strip('"'), line, command_directory))
        executable_names = {Path(token).name.casefold() for token in analysis["executables"]}
        if any(_gcc_name_role(name) == "ar" for name in executable_names):
            archive = next((token for token in tokens[1:] if Path(token).suffix.casefold() in {".a", ".lib"}), None)
            if archive:
                candidates.append((archive, line, command_directory))

    artifacts: list[dict[str, Any]] = []
    for raw_path, line, command_directory in candidates:
        candidate = Path(raw_path)
        path = candidate if candidate.is_absolute() else command_directory / candidate
        if not path.is_file() and not candidate.is_absolute() and build_directory.is_dir():
            matches = [item for item in build_directory.rglob(candidate.name) if item.is_file()]
            if len(matches) == 1:
                path = matches[0]
        suffix = path.suffix.casefold()
        is_versioned_so = ".so." in path.name.casefold()
        is_extensionless_executable = not suffix
        if (
            not path.is_file()
            or suffix in excluded_suffixes
            or not (suffix in suffixes or is_versioned_so or is_extensionless_executable)
        ):
            continue
        resolved = pathutil.canonical(path)
        if any(pathutil.same_path(resolved, item["path"]) for item in artifacts):
            continue
        stat = resolved.stat()
        artifacts.append(
            {
                "path": str(resolved),
                "size": stat.st_size,
                "modifiedNs": stat.st_mtime_ns,
                "sha256": pathutil.sha256_file(resolved),
                "provenance": "ORIGINAL_BUILD_COMMAND_OUTPUT",
                "line": line[:1000],
            }
        )
    return artifacts

def reject_cover_wrappers(tools: dict[str, Path]) -> None:
    install = os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    install_path = pathutil.canonical(install, False) if install else None
    offenders: list[str] = []
    for path in tools.values():
        if path.parent.name.casefold() == "wrapper" or (install_path and pathutil.path_within(path, install_path)):
            offenders.append(str(path))
    if offenders:
        raise WorkflowWaiting(
            "ORIGINAL_BUILD_USED_COVER_WRAPPER",
            "The supposed original build invoked COVER wrapper executables",
            {
                "originalBuildPlan": [],
                "originalBuildCommandSource": ["MODEL_INFERRED"],
            },
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
            evidence=offenders,
        )

def execute_gcc(
    job: dict[str, Any],
    selection: dict[str, Any],
    build_tool: str,
    configure_arguments: list[str],
    targets: list[str],
    parallel: int,
    clean_target: str | None,
    timeout: int = 3600,
    original_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logs = Path(job["directories"]["logs"])
    plan = original_plan or create_gcc_original_plan(
        job,
        selection,
        build_tool,
        configure_arguments,
        targets,
        parallel,
        clean_target,
        [selection["entry"]],
        timeout,
    )
    plan = build_plan.validate_original_build_plan(job, plan, selection["driver"])
    commands = [
        (phase, plan["commands"][phase])
        for phase in ("configure", "clean", "rebuild", "build")
        if phase in plan["commands"]
    ]
    cache_path = (
        Path(plan["originalBuildDirectory"]) / "CMakeCache.txt"
        if selection["driver"] == "CMAKE"
        else None
    )

    results: dict[str, dict[str, Any]] = {}
    outputs: dict[str, Path] = {}
    cmake_reset = None
    if selection["driver"] == "CMAKE" and "configure" in plan["commands"]:
        cmake_reset = _reset_original_cmake_build_directory(
            job,
            Path(plan["originalBuildDirectory"]),
        )
    for name, spec in commands:
        result = _run(
            spec,
            logs / f"original-{selection['driver'].casefold()}-{name}.log",
            "ORIGINAL_BUILD_FAILED",
            f"The original {selection['driver']} {name} command failed",
        )
        outputs[name] = Path(result["log"])
        results[name] = _without_output(result)
    if cache_path is not None and not cache_path.is_file():
        raise errors.ExecutorError(
            "ORIGINAL_CMAKE_CACHE_MISSING",
            "The original CMake configure succeeded without producing CMakeCache.txt",
            [str(cache_path)],
        )
    cmake_values = project_files.parse_cmake_cache(cache_path) if cache_path is not None else {}
    build_output = outputs.get("rebuild", outputs.get("build"))
    if build_output is None:
        raise errors.ExecutorError("ORIGINAL_BUILD_LOG_MISSING", "The original build produced no parseable log")
    build_phase = "rebuild" if "rebuild" in plan["commands"] else "build"
    build_directory = Path(plan["originalBuildDirectory"])
    try:
        toolchain = extract_gcc_toolchain(
            build_output,
            None,
            list(plan["requiredRoles"]),
            environment=_command_environment(plan["commands"][build_phase]),
            selected_tools=plan.get("targetTools"),
            working_directory=build_directory,
        )
    except WorkflowWaiting as error:
        command_spec = plan["commands"].get("commands")
        if error.code != "ORIGINAL_TOOLCHAIN_UNRESOLVED" or command_spec is None:
            raise
        command_result = _run(
            command_spec,
            logs / f"original-{selection['driver'].casefold()}-commands.log",
            "ORIGINAL_TOOLCHAIN_COMMANDS_FAILED",
            f"The optional {selection['driver']} tool-command query failed after the original build",
        )
        outputs["commands"] = Path(command_result["log"])
        results["commands"] = _without_output(command_result)
        toolchain = extract_gcc_toolchain(
            build_output,
            outputs["commands"],
            list(plan["requiredRoles"]),
            environment=_command_environment(command_spec),
            selected_tools=plan.get("targetTools"),
            working_directory=build_directory,
        )
    artifacts = extract_gcc_artifacts(build_output, build_directory)
    return envelopes.envelope(
        "EXECUTING_ORIGINAL_BUILD",
        "SUCCEEDED",
        {
            "driver": selection["driver"],
            "entry": selection["entry"],
            "buildDirectory": str(pathutil.canonical(build_directory, False)),
            "commands": results,
            "cmakeReset": cmake_reset,
            "cache": str(cache_path) if cache_path else None,
            "generator": cmake_values.get("CMAKE_GENERATOR"),
            "originalBuildPlan": plan,
            "toolchain": toolchain,
            "artifacts": artifacts,
        },
    )
