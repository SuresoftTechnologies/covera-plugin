from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any


from cover_build.core.errors import WorkflowWaiting
from cover_build.planning.selection import select_one
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import project_files
from cover_build.core import errors


SOURCE_SUFFIXES = {
    "c_compiler": {".c"},
    "cxx_compiler": {".cc", ".cp", ".cpp", ".cxx", ".c++"},
}
PRESERVED_CMAKE_KEYS = (
    "CMAKE_TOOLCHAIN_FILE",
    "CMAKE_SYSROOT",
    "CMAKE_FIND_ROOT_PATH",
    "CMAKE_PREFIX_PATH",
    "CMAKE_C_FLAGS",
    "CMAKE_CXX_FLAGS",
    "CMAKE_EXE_LINKER_FLAGS",
    "CMAKE_SHARED_LINKER_FLAGS",
    "CMAKE_MAKE_PROGRAM",
    "CMAKE_GENERATOR_INSTANCE",
)
PRESERVED_CMAKE_TYPES = {"BOOL", "STRING", "PATH", "FILEPATH"}
SECRET_KEY_PATTERN = re.compile(
    r"(?:^|[_-])(?:PASSWORD|PASSWD|TOKEN|SECRET|PRIVATE[_-]?KEY|SIGNING[_-]?KEY|ACCESS[_-]?KEY|API[_-]?KEY|CREDENTIAL|AUTH|AUTHENTICATION|AUTHORIZATION|CLIENT[_-]?SECRET)(?:$|[_-])",
    flags=re.IGNORECASE,
)


def _preserved_cmake_arguments(cache_path: Path | None) -> list[str]:
    if cache_path is None:
        return []
    arguments: list[str] = []
    for key, entry in project_files.parse_cmake_cache_entries(cache_path).items():
        value_type = entry["type"].upper()
        value = entry["value"]
        if value_type not in PRESERVED_CMAKE_TYPES or not value or value.endswith("-NOTFOUND"):
            continue
        if SECRET_KEY_PATTERN.search(key):
            continue
        if key.startswith("CMAKE_") and key not in PRESERVED_CMAKE_KEYS:
            continue
        arguments.append(f"-D{key}:{value_type}={value}")
    return arguments


def _source_roles(project_root: Path, discovery: dict[str, Any] | None = None) -> list[str]:
    indexed = set((discovery or {}).get("data", {}).get("sourceRoles") or [])
    indexed_roles = [
        role for role, language in (("c_compiler", "C"), ("cxx_compiler", "CXX"))
        if language in indexed
    ]
    if indexed_roles:
        return indexed_roles
    roles: set[str] = set()
    for current_root, directory_names, file_names in os.walk(project_root):
        directory_names[:] = [name for name in directory_names if name.casefold() not in project_files.SKIP_DIRS]
        for file_name in file_names:
            suffix = Path(file_name).suffix.casefold()
            for role, suffixes in SOURCE_SUFFIXES.items():
                if suffix in suffixes:
                    roles.add(role)
        if roles == set(SOURCE_SUFFIXES):
            break
    if not roles:
        raise WorkflowWaiting(
            "GCC_SOURCE_LANGUAGE_UNRESOLVED",
            "No C or C++ source suffix was found to derive required compiler wrapper roles",
            {"requiredRoles": sorted(SOURCE_SUFFIXES)},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
        )
    return sorted(roles)


def _cmake_source_candidates(discovery: dict[str, Any]) -> list[str]:
    return [item["path"] for item in discovery["data"]["files"] if item["type"] == "CMAKE_PROJECT"]


def _matching_cmake_caches(discovery: dict[str, Any], source: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cache in discovery["data"].get("cmakeCaches", []):
        if not cache.get("validForProjectRoot", False):
            continue
        home = cache.get("values", {}).get("CMAKE_HOME_DIRECTORY")
        if home and pathutil.same_path(home, source):
            result.append(cache)
    return result


def _normal_cmake_build_directory(job: dict[str, Any], source: Path) -> Path | None:
    """Return an evidenced conventional project build path when available."""
    candidates = [source / "build", Path(job["projectRoot"]) / "build"]
    existing = []
    for candidate in candidates:
        resolved = pathutil.canonical(candidate, False)
        if (
            resolved.is_dir()
            and pathutil.path_within(resolved, job["projectRoot"])
            and resolved not in existing
        ):
            existing.append(resolved)
    if len(existing) == 1:
        return existing[0]
    # A top-level CMake project commonly creates ``<source>/build`` during the
    # original configure.  This is still the customer's normal output path, not
    # a job-local fallback.  Do not guess it for nested/ambiguous source trees.
    if not existing and pathutil.same_path(source, job["projectRoot"]):
        return pathutil.canonical(source / "build", False)
    return None


def select_cmake_generator(explicit: str | None, cache_values: dict[str, str]) -> tuple[str | None, str]:
    """Preserve an evidenced CMake generator without treating it as a build driver."""
    if explicit:
        return explicit, "EXPLICIT_ARGUMENT"
    if cache_values.get("CMAKE_GENERATOR"):
        return cache_values["CMAKE_GENERATOR"], "PROJECT_CACHE"
    return None, "CMAKE_DEFAULT"


def select_gcc_build(
    job: dict[str, Any],
    discovery: dict[str, Any],
    build_system: str,
    choices: dict[str, Any],
    explicit_build_directory: str | None = None,
    explicit_generator: str | None = None,
    original_plan: dict[str, Any] | None = None,
    explicit_configuration: str | None = None,
) -> dict[str, Any]:
    if build_system == "CMAKE":
        saved_path = Path(job["directories"]["temporary"]) / "original-cmake-selection.json"
        if saved_path.is_file():
            saved = fileio.load_json(saved_path)
            fileio.verify_integrity(saved)
            if saved.get("jobId") != job.get("jobId"):
                raise errors.ExecutorError("ORIGINAL_BUILD_PLAN_JOB_MISMATCH", "CMake selection belongs to another job")
            selected = saved["selection"]
            requested = {
                "buildDirectory": explicit_build_directory or choices.get("gccBuildDirectory") or choices.get("cmakeEvidenceBuildDirectory"),
                "entry": choices.get("gccBuildEntry"),
                "generator": explicit_generator or choices.get("gccGenerator"),
                "configuration": explicit_configuration,
            }
            if original_plan:
                for field, key in (("buildDirectory", "originalBuildDirectory"), ("entry", "adapterEntry"), ("generator", "generator"), ("configuration", "configuration")):
                    value = original_plan.get(key)
                    if value is not None:
                        _check_cmake_selection(field, requested.get(field), value)
                        requested[field] = value
            for field, value in requested.items():
                _check_cmake_selection(field, value, selected.get(field))
            return dict(selected)
        if original_plan:
            _check_cmake_selection("buildDirectory", explicit_build_directory or choices.get("gccBuildDirectory"), original_plan["originalBuildDirectory"])
            explicit_build_directory = original_plan["originalBuildDirectory"]
            choices = {**choices, "gccBuildEntry": original_plan["adapterEntry"]}
            _check_cmake_selection("generator", explicit_generator, original_plan.get("generator"))
            explicit_generator = original_plan.get("generator") or explicit_generator
            _check_cmake_selection("configuration", explicit_configuration, original_plan.get("configuration"))
            explicit_configuration = original_plan.get("configuration") or explicit_configuration
        explicit_build_directory = explicit_build_directory or choices.get("gccBuildDirectory")
        sources = _cmake_source_candidates(discovery)
        if not sources:
            cache_homes = sorted(
                {
                    cache.get("values", {}).get("CMAKE_HOME_DIRECTORY")
                    for cache in discovery["data"].get("cmakeCaches", [])
                    if cache.get("validForProjectRoot", False)
                    and cache.get("values", {}).get("CMAKE_HOME_DIRECTORY")
                },
                key=str.casefold,
            )
            sources = [str(Path(value) / "CMakeLists.txt") for value in cache_homes]
        entry = Path(select_one("gccBuildEntry", sources, choices))
        source = entry.parent
        if not pathutil.path_within(source, job["projectRoot"]):
            raise errors.ExecutorError(
                "CMAKE_SOURCE_OUTSIDE_PROJECT",
                "Selected CMake source is outside projectRoot",
                [str(source), str(job["projectRoot"])],
            )
        caches = _matching_cmake_caches(discovery, source)
        if explicit_build_directory:
            build_directory = pathutil.canonical(explicit_build_directory, False)
            if not (
                pathutil.path_within(build_directory, job["projectRoot"])
                or pathutil.path_within(build_directory, job["jobDirectory"])
            ):
                raise errors.ExecutorError(
                    "CMAKE_EXISTING_BUILD_DIRECTORY_UNSAFE",
                    "The selected CMake build directory must be under projectRoot or the current job",
                    [str(build_directory)],
                )
            selected_cache = next(
                (cache for cache in caches if pathutil.same_path(Path(cache["path"]).parent, build_directory)),
                None,
            )
        elif len(caches) > 1:
            candidates = [str(Path(cache["path"]).parent) for cache in caches]
            evidence_directory = Path(select_one("cmakeEvidenceBuildDirectory", candidates, choices))
            selected_cache = next(cache for cache in caches if pathutil.same_path(Path(cache["path"]).parent, evidence_directory))
            build_directory = evidence_directory
        else:
            selected_cache = caches[0] if caches else None
            build_directory = Path(selected_cache["path"]).parent if selected_cache else _normal_cmake_build_directory(job, source)
            if build_directory is None:
                raise WorkflowWaiting(
                    "CMAKE_BUILD_DIRECTORY_UNRESOLVED",
                    "The original CMake build directory is not evidenced; do not create coverage outputs in a job-local directory",
                    {"gccBuildDirectory": "provide the customer's normal CMake -B/build directory"},
                    decision_owner="MODEL",
                    purpose="ORIGINAL_BUILD_COMMAND",
                )
        values = selected_cache.get("values", {}) if selected_cache else {}
        decision_generator = choices.get("gccGenerator")
        if decision_generator is not None and not isinstance(decision_generator, str):
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "gccGenerator must be a string")
        generator, generator_source = select_cmake_generator(explicit_generator or decision_generator, values)
        preserved_arguments = _preserved_cmake_arguments(Path(selected_cache["path"]) if selected_cache else None)
        return {
            "driver": "CMAKE",
            "backend": "CMAKE",
            "entry": str(entry),
            "sourceDirectory": str(source),
            "buildDirectory": str(build_directory),
            "originalBuildDirectory": str(build_directory),
            "generator": generator,
            "generatorSource": generator_source,
            "configuration": explicit_configuration or values.get("CMAKE_BUILD_TYPE") or None,
            "preservedConfigureArgs": preserved_arguments,
            "cache": selected_cache["path"] if selected_cache else None,
            "requiredRoles": _source_roles(Path(job["projectRoot"]), discovery),
        }
    if build_system != "MAKE":
        raise errors.ExecutorError("UNSUPPORTED_BUILD_SYSTEM", f"Unsupported GCC build driver: {build_system}")
    entries = discovery["data"].get("gccBuildDrivers", {}).get(build_system, [])
    entry = Path(select_one("gccBuildEntry", entries, choices))
    return {
        "driver": build_system,
        "backend": build_system,
        "entry": str(entry),
        "sourceDirectory": str(Path(job["projectRoot"])),
        "buildDirectory": str(entry.parent),
        "generator": None,
        "configuration": None,
        "preservedConfigureArgs": [],
        "cache": None,
        "requiredRoles": _source_roles(Path(job["projectRoot"]), discovery),
    }


def _check_cmake_selection(field: str, requested: Any, original: Any) -> None:
    if requested is None:
        return
    equal = pathutil.same_path(requested, original) if original and field in {"buildDirectory", "entry"} else requested == original
    if not equal:
        code = "COVERAGE_OUTPUT_PATH_CHANGED" if field == "buildDirectory" else "CMAKE_ORIGINAL_SELECTION_CONFLICT"
        raise errors.ExecutorError(code, f"CMake {field} conflicts with the original build; start a new job to change it", [requested, original])


def save_cmake_selection(job: dict[str, Any], selection: dict[str, Any]) -> None:
    """Keep the original selection independent of caches changed by coverage."""
    fileio.atomic_write_json(
        Path(job["directories"]["temporary"]) / "original-cmake-selection.json",
        fileio.add_integrity({"jobId": job["jobId"], "selection": selection}),
    )


def infer_expected_artifacts(
    job: dict[str, Any],
    selection: dict[str, Any],
    native_build: dict[str, Any],
) -> list[str]:
    """Map observed original-build outputs to the equivalent coverage outputs."""
    driver = selection["driver"]
    original_directory_value = native_build.get("buildDirectory")
    original_directory = (
        pathutil.canonical(original_directory_value, False)
        if original_directory_value
        else None
    )
    coverage_directory = pathutil.canonical(selection["buildDirectory"], False)
    if driver == "CMAKE" and original_directory and not pathutil.same_path(original_directory, coverage_directory):
        raise errors.ExecutorError(
            "COVERAGE_OUTPUT_PATH_CHANGED",
            "CMake coverage must reuse the original build directory so Module buildOutputPath remains unchanged",
            [str(original_directory), str(coverage_directory)],
        )
    result: list[str] = []

    for artifact in native_build.get("artifacts") or []:
        if not isinstance(artifact, dict) or artifact.get("provenance") != "ORIGINAL_BUILD_COMMAND_OUTPUT":
            continue
        value = artifact.get("path")
        if not isinstance(value, str) or not value:
            continue
        original_path = pathutil.canonical(value, False)
        expected_path = original_path
        if driver == "CMAKE" and original_directory and pathutil.path_within(original_path, original_directory):
            relative = original_path.relative_to(original_directory)
            if any(part.casefold() == "cmakefiles" for part in relative.parts):
                continue
        expected = str(pathutil.canonical(expected_path, False))
        if not any(pathutil.same_path(expected, existing) for existing in result):
            result.append(expected)
    return result


def _program_candidates(driver: str, selection: dict[str, Any], explicit: str | None) -> list[str]:
    candidates: list[str] = []
    if explicit:
        candidates.append(str(pathutil.canonical(explicit)))
    if driver == "CMAKE" and selection.get("cache"):
        values = project_files.parse_cmake_cache(Path(selection["cache"]))
        if values.get("CMAKE_COMMAND"):
            candidates.append(str(pathutil.canonical(values["CMAKE_COMMAND"])))
    environment_name = {"CMAKE": "CMAKE", "MAKE": "MAKE"}[driver]
    if os.environ.get(environment_name):
        candidates.append(str(pathutil.canonical(os.environ[environment_name])))
    names = {
        "CMAKE": ["cmake"],
        "MAKE": ["make", "mingw32-make", "gmake"],
    }[driver]
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(str(pathutil.canonical(found)))
    unique: list[str] = []
    for candidate in candidates:
        if not any(pathutil.same_path(candidate, existing) for existing in unique):
            unique.append(candidate)
    return unique


def select_build_tool(driver: str, selection: dict[str, Any], choices: dict[str, Any], explicit: str | None = None) -> str:
    candidates = _program_candidates(driver, selection, explicit)
    supplied = choices.get("gccBuildTool")
    if supplied is not None and not candidates:
        source = choices.get("originalBuildCommandSource")
        if source not in {"MODEL_INFERRED", "USER_CONFIRMED_FALLBACK"}:
            raise errors.ExecutorError(
                "INVALID_MODEL_BUILD_COMMAND",
                "A non-discovered build tool requires originalBuildCommandSource",
                ["MODEL_INFERRED", "USER_CONFIRMED_FALLBACK"],
            )
        return str(pathutil.canonical(supplied))
    if not candidates:
        raise WorkflowWaiting(
            "GCC_BUILD_TOOL_NOT_FOUND",
            f"No executable candidate was found for the {driver} build driver",
            {"gccBuildTool": []},
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
        )
    if explicit:
        selected = str(pathutil.canonical(explicit))
        if selected not in candidates:
            raise errors.ExecutorError("INVALID_GCC_BUILD_TOOL", "Explicit GCC build tool was not retained as a candidate", candidates)
        return selected
    return select_one("gccBuildTool", candidates, choices)


def select_wrapper_set(
    agent_descriptor: dict[str, Any],
    choices: dict[str, Any],
    expected_prefix: str | None = None,
    toolchain: dict[str, Any] | None = None,
    wrapper_mapping: dict[str, Path] | None = None,
) -> dict[str, str]:
    if toolchain is not None and wrapper_mapping is not None:
        return select_observed_wrapper_set(agent_descriptor, toolchain, wrapper_mapping, expected_prefix or "")
    wrapper_directory = Path(agent_descriptor["toolchainProvisioning"]["wrapperDirectory"])
    suffix = ".exe" if os.name == "nt" else ""
    files = [item for item in wrapper_directory.iterdir() if item.is_file()]
    if expected_prefix == "MSVC":
        by_name = {item.name.casefold(): item for item in files}
        missing = [name for name in ("cl.exe", "link.exe", "lib.exe") if name not in by_name]
        if missing:
            raise WorkflowWaiting(
                "MSVC_WRAPPER_SET_NOT_FOUND",
                "The selected Agent has no complete CL/link/lib wrapper set for the CMake toolchain",
                {"requiredWrappers": missing},
                decision_owner="USER",
                purpose="CUSTOMER_CONFIGURATION",
            )
        compiler = str(pathutil.canonical(by_name["cl.exe"]))
        return {
            "c_compiler": compiler,
            "cxx_compiler": compiler,
            "ar": str(pathutil.canonical(by_name["lib.exe"])),
            "linker": str(pathutil.canonical(by_name["link.exe"])),
        }
    prefixes: dict[str, dict[str, str]] = {}
    for item in files:
        name = item.name
        stem = name[:-4] if suffix and name.casefold().endswith(suffix) else name
        match = re.fullmatch(r"(.*)gcc", stem, flags=re.IGNORECASE)
        if not match:
            continue
        prefix = match.group(1)
        expected = {
            "c_compiler": wrapper_directory / f"{prefix}gcc{suffix}",
            "cxx_compiler": wrapper_directory / f"{prefix}g++{suffix}",
            "ar": wrapper_directory / f"{prefix}ar{suffix}",
            "linker": wrapper_directory / f"{prefix}ld{suffix}",
        }
        if all(path.is_file() for path in expected.values()):
            prefixes[prefix] = {role: str(pathutil.canonical(path)) for role, path in expected.items()}
    if not prefixes:
        raise WorkflowWaiting(
            "GCC_WRAPPER_SET_NOT_FOUND",
            "No complete gcc/g++/ar/ld wrapper set exists under the selected Agent",
            {"gccWrapperPrefix": []},
            decision_owner="USER",
            purpose="CUSTOMER_CONFIGURATION",
        )
    if expected_prefix is not None:
        matching = [prefix for prefix in prefixes if prefix.casefold() == expected_prefix.casefold()]
        if not matching:
            raise WorkflowWaiting(
                "GCC_WRAPPER_SET_NOT_FOUND",
                "The selected Agent has no wrapper set matching the original GCC target prefix",
                {"gccWrapperPrefix": sorted(prefixes)},
                decision_owner="USER",
                purpose="CUSTOMER_CONFIGURATION",
                evidence=[{"originalPrefix": expected_prefix}],
            )
        prefix = matching[0]
        supplied = choices.get("gccWrapperPrefix")
        if supplied is not None and supplied.casefold() != prefix.casefold():
            raise errors.ExecutorError(
                "INVALID_WORKFLOW_DECISION",
                "gccWrapperPrefix conflicts with the original toolchain prefix",
                [prefix],
            )
    else:
        prefix = select_one("gccWrapperPrefix", sorted(prefixes), choices)
    return prefixes[prefix]


def select_observed_wrapper_set(
    agent_descriptor: dict[str, Any],
    toolchain: dict[str, Any],
    wrapper_mapping: dict[str, Path],
    expected_prefix: str,
) -> dict[str, str]:
    """Select wrappers only for roles actually observed in the original build."""
    wrapper_directory = Path(agent_descriptor["toolchainProvisioning"]["wrapperDirectory"])
    role_paths = toolchain.get("tools", {})
    supported_roles = {"c_compiler", "cxx_compiler", "ar", "linker"}
    result: dict[str, str] = {}
    for role, real_value in role_paths.items():
        if role not in supported_roles:
            continue
        invoked_path = Path(str(real_value))
        real_path = pathutil.canonical(real_value, False)
        invoked_name = invoked_path.name.casefold()
        if os.name == "nt" and not invoked_name.endswith(".exe"):
            invoked_name = f"{invoked_name}.exe"
        candidates = [
            name
            for name, mapped_path in wrapper_mapping.items()
            if (
                (invoked_path.is_absolute() or invoked_path.parent != Path("."))
                and pathutil.same_path(real_path, mapped_path)
            )
            or name.casefold() == invoked_name
        ]
        if len(candidates) != 1:
            raise WorkflowWaiting(
                "ORIGINAL_TOOL_WRAPPER_UNRESOLVED",
                "The observed original tool does not resolve to exactly one selected COVER wrapper",
                {"originalToolPath": [str(real_value)]},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
                evidence=[{"role": role, "wrapperCandidates": sorted(candidates)}],
            )
        wrapper_name = candidates[0]
        wrapper = wrapper_directory / wrapper_name
        if not wrapper.is_file():
            raise WorkflowWaiting(
                "GCC_WRAPPER_SET_NOT_FOUND",
                "The selected Agent has no file for the selected COVER wrapper mapping",
                {"requiredWrappers": [wrapper_name]},
                decision_owner="USER",
                purpose="CUSTOMER_CONFIGURATION",
                evidence=[{"role": role, "originalToolPath": real_value}],
            )
        result[role] = str(pathutil.canonical(wrapper))
    if not result:
        raise WorkflowWaiting(
            "ORIGINAL_TOOL_WRAPPER_UNRESOLVED",
            "No observed original native tool resolves to a COVER wrapper",
            {"originalToolPath": []},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
        )
    return result


def wrapper_real_tools(agent_descriptor: dict[str, Any], wrappers: dict[str, str]) -> dict[str, str]:
    info_directory_value = agent_descriptor["toolchainProvisioning"].get("toolchainInfoDirectory")
    if not info_directory_value:
        raise WorkflowWaiting(
            "AGENT_TOOLCHAIN_UNPROVISIONED",
            "The Agent has no toolchaininfo directory for the selected GCC wrappers",
            {"agentProvisioning": "synchronize Agent toolchains, then resume"},
            decision_owner="USER",
            purpose="CUSTOMER_CONFIGURATION",
        )
    info_directory = Path(info_directory_value)
    result: dict[str, str] = {}
    for wrapper in wrappers.values():
        mapping = info_directory / Path(wrapper).name
        if not mapping.is_file():
            raise WorkflowWaiting(
                "AGENT_TOOLCHAIN_UNPROVISIONED",
                f"Missing Agent mapping for {Path(wrapper).name}",
                {"agentProvisioning": "synchronize Agent toolchains, then resume"},
                decision_owner="USER",
                purpose="CUSTOMER_CONFIGURATION",
            )
        value = mapping.read_text(encoding="utf-8-sig", errors="replace").strip().strip('"')
        result[Path(wrapper).name] = str(pathutil.canonical(value))
    return result
