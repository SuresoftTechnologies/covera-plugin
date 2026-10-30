"""Map GCC wrappers and execute the coverage rebuild."""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


from cover_build.core.errors import WorkflowWaiting
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import gcc_tools
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors


def gcc_wrapper_mapping(
    toolchain: dict[str, Any],
    *,
    search_directories: list[str | Path] | None = None,
) -> tuple[dict[str, Path], str]:
    """Map only tools observed in the original build to same-name wrappers."""
    directories: list[Path] = []

    def add_directory(value: str | Path) -> None:
        directory = Path(value).expanduser().resolve()
        if directory.is_dir() and not any(pathutil.same_path(directory, item) for item in directories):
            directories.append(directory)

    for directory in search_directories or []:
        add_directory(directory)

    def resolve_tool(value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute() or candidate.parent != Path("."):
            return candidate
        candidate_names = [candidate.name]
        if os.name == "nt" and not candidate.name.casefold().endswith(".exe"):
            candidate_names.append(f"{candidate.name}.exe")
        matches = [
            directory / name
            for directory in directories
            for name in candidate_names
            if (directory / name).is_file()
        ]
        unique: list[Path] = []
        for match in matches:
            resolved = match.resolve()
            if not any(pathutil.same_path(resolved, item) for item in unique):
                unique.append(resolved)
        if len(unique) > 1:
            raise WorkflowWaiting(
                "ORIGINAL_TOOLCHAIN_AMBIGUOUS",
                "The original tool basename resolves to more than one executable",
                {"originalToolPath": [str(path) for path in unique]},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
                evidence=[{"invokedTool": value, "searchDirectories": [str(path) for path in directories]}],
            )
        return unique[0] if unique else candidate

    role_paths = {role: resolve_tool(str(path)) for role, path in toolchain["tools"].items()}
    family = toolchain.get("family")
    if family is None:
        compiler_names = {
            path.name.casefold()
            for role, path in role_paths.items()
            if role in {"c_compiler", "cxx_compiler"}
        }
        family = "MSVC" if compiler_names and compiler_names <= {"cl.exe", "clang-cl.exe"} else "GCC"
    if family == "MSVC":
        compilers = {
            path
            for role, path in role_paths.items()
            if role in {"c_compiler", "cxx_compiler"}
        }
        if len(compilers) != 1:
            raise WorkflowWaiting(
                "ORIGINAL_TOOLCHAIN_PREFIX_AMBIGUOUS",
                "The observed C and C++ compilation does not resolve to one MSVC-compatible compiler",
                {"originalToolPath": sorted(str(path) for path in compilers)},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
            )
        compiler = next(iter(compilers))
        mapping = {"CL.exe": compiler}
        if "linker" in role_paths:
            mapping["link.exe"] = role_paths["linker"]
        if "ar" in role_paths:
            mapping["lib.exe"] = role_paths["ar"]
        return mapping, "MSVC"

    suffix = ".exe" if os.name == "nt" else ""
    mapping: dict[str, Path] = {}
    role_suffix = {
        "c_compiler": "gcc",
        "cxx_compiler": "g++",
        "ar": "ar",
        "linker": "ld",
    }
    compiler_prefix = toolchain.get("gccTargetPrefix")
    if not isinstance(compiler_prefix, str):
        compiler_prefixes: set[str] = set()
        # Compatibility fallback for jobs created before gccTargetPrefix.
        for role, path in role_paths.items():
            if role not in {"c_compiler", "cxx_compiler"}:
                continue
            compiler_name = Path(path).name
            if suffix and compiler_name.casefold().endswith(suffix):
                compiler_name = compiler_name[:-4]
            compiler_name = re.sub(r"-(?:\d+(?:\.\d+)*)$", "", compiler_name)
            name_suffix = role_suffix[role]
            if compiler_name.casefold().endswith(name_suffix):
                compiler_prefixes.add(compiler_name[: -len(name_suffix)].casefold())
        compiler_prefix = next(iter(compiler_prefixes)) if len(compiler_prefixes) == 1 else None
    if compiler_prefix is None:
        raise WorkflowWaiting(
            "ORIGINAL_TOOLCHAIN_PREFIX_AMBIGUOUS",
            "The observed C and C++ compilers do not share one GCC target prefix",
            {"gccWrapperPrefix": []},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
        )
    def add_mapping(name: str, path: Path) -> None:
        # Windows lookup may return PATHEXT's uppercase .EXE. Keep the
        # original tool path intact and normalize only the wrapper suffix.
        if os.name == "nt" and name.casefold().endswith(".exe"):
            name = name[:-4] + ".exe"
        existing = mapping.get(name)
        if existing is not None and not pathutil.same_path(existing, path):
            raise WorkflowWaiting(
                "ORIGINAL_TOOLCHAIN_WRAPPER_NAME_COLLISION",
                "Different original tools normalize to the same COVER wrapper name",
                {"originalToolPath": sorted({str(existing), str(path)})},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
                evidence=[{"wrapperName": name}],
            )
        mapping[name] = path

    unsupported_roles = sorted(set(role_paths) - set(role_suffix))
    if unsupported_roles:
        raise errors.ExecutorError(
            "INVALID_GCC_ROLE",
            "The original build reported unsupported native tool roles",
            unsupported_roles,
        )
    for path in role_paths.values():
        # gcc-ar is not interchangeable with ar. Likewise, a link performed
        # by gcc/g++ reuses that exact compiler-driver wrapper.
        add_mapping(path.name, path)
    return mapping, compiler_prefix

def _execute_gcc_plan_core(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    plan_path: Path,
    adapter: str,
    artifacts: list[str],
    execute: bool,
    initial_stage: int = 12,
) -> dict[str, Any]:
    if not execute:
        raise WorkflowWaiting(
            "EXECUTION_NOT_ENABLED",
            "Plan is ready; build execution was not enabled",
            {"execute": True},
            decision_owner="USER",
            purpose="MUTATION_PERMISSION",
        )
    if not artifacts:
        raise WorkflowWaiting(
            "EXPECTED_ARTIFACT_REQUIRED",
            "At least one expected GCC build artifact is required before clean/build execution",
            {"expectedArtifacts": []},
            decision_owner="MODEL",
            purpose="BUILD_OUTPUT",
        )
    coverage_plan = fileio.load_json(plan_path)
    comparison = pathutil.require_same_output_paths(
        list(artifacts),
        list(coverage_plan.get("expectedArtifacts") or artifacts),
    )
    before_path = Path(job["directories"]["temporary"]) / "artifacts-before.json"
    after_path = Path(job["directories"]["temporary"]) / "artifacts-after.json"
    next_stage = initial_stage
    if adapter in {"CMAKE", "CMAKE_GCC"}:
        record(
            job,
            stages,
            next_stage,
            "cmake-configure",
            processes.execute_plan(SimpleNamespace(plan=str(plan_path), phase="configure", log_dir=job["directories"]["logs"])),
        )
        next_stage += 1
        plan = fileio.load_json(plan_path)
        cache_path = Path(plan["buildDirectory"]) / "CMakeCache.txt"
        record(
            job,
            stages,
            next_stage,
            "verify-cmake-cache",
            gcc_tools.verify_cmake_cache(SimpleNamespace(plan=str(plan_path), cache=str(cache_path), output=None)),
        )
        next_stage += 1
    else:
        record(
            job,
            stages,
            next_stage,
            "gcc-command-preflight",
            envelopes.envelope(
                "DEFERRING_GCC_COMMAND_PREFLIGHT",
                "SKIPPED",
                {"policy": "RUN_ONLY_AFTER_WRAPPER_SELECTION_FAILURE"},
            ),
        )
        next_stage += 1
    record(
        job,
        stages,
        next_stage,
        "snapshot-before",
        processes.snapshot_artifacts(SimpleNamespace(artifact=artifacts, output=str(before_path))),
    )
    plan_commands = fileio.load_json(plan_path).get("commands", {})
    has_clean = "clean" in plan_commands
    if has_clean:
        record(
            job,
            stages,
            next_stage + 1,
            "clean",
            processes.execute_plan(SimpleNamespace(plan=str(plan_path), phase="clean", log_dir=job["directories"]["logs"])),
        )
    build_stage = next_stage + 2 if has_clean else next_stage + 1
    build_phase = "rebuild" if "rebuild" in plan_commands else "build"
    build_result = record(
        job,
        stages,
        build_stage,
        build_phase,
        processes.execute_plan(SimpleNamespace(plan=str(plan_path), phase=build_phase, log_dir=job["directories"]["logs"])),
    )
    record(
        job,
        stages,
        build_stage + 1,
        "snapshot-after",
        processes.snapshot_artifacts(SimpleNamespace(artifact=artifacts, output=str(after_path))),
    )
    artifact_result = processes.verify_artifacts(SimpleNamespace(before=str(before_path), after=str(after_path), output=None))
    artifact_result.setdefault("data", {})["outputPathComparison"] = comparison
    artifact_result = record(job, stages, build_stage + 2, "verify-artifacts", artifact_result)
    command_results = build_result.get("data", {}).get("results", [])
    if not command_results:
        raise errors.ExecutorError("BUILD_INTERVAL_UNAVAILABLE", "The GCC build stage emitted no command timing evidence")
    return {
        "artifactVerification": artifact_result,
        "lastStage": build_stage + 2,
        "artifacts": list(artifacts),
        "outputPathComparison": comparison,
        "buildInterval": {
            "startedAt": command_results[0]["startedAt"],
            "finishedAt": command_results[-1]["finishedAt"],
        },
    }

def execute_gcc_plan(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    plan_path: Path,
    adapter: str,
    artifacts: list[str],
    execute: bool,
) -> dict[str, Any]:
    """Execute a validated GCC-family coverage plan."""
    return _execute_gcc_plan_core(job, stages, record, plan_path, adapter, artifacts, execute)
