"""COVER 빌드 계획 검증·실행과 공유 CMake 빌드 상태 초기화."""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Any

from cover_build.common import envelopes, fileio, pathutil, processes
from cover_build.core import errors
from cover_build.services import toolchains

PLAN_SCHEMA = "cover-build-plan/v1"


def reset_shared_coverage_build_directory(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Reset only generated build state before a shared coverage configure.

    Native and coverage builds deliberately use the same project build
    directory.  CMake's cache records the compiler and linker, so a clean
    target alone is insufficient when the native build ran first.  Keep the
    reset narrow and fail closed: the selected directory must be a child of
    the project root, and only well-known CMake-generated entries are removed.
    """
    if plan.get("reuseOriginalBuildDirectory") is not True:
        return None
    adapter = plan.get("adapter")
    if adapter not in {"CMAKE", "CMAKE_GCC"}:
        return {"status": "NOT_REQUIRED", "adapter": adapter}
    project_root = pathutil.canonical(str(plan.get("projectRoot") or ""))
    build_directory = pathutil.canonical(str(plan.get("buildDirectory") or ""), False)
    if not project_root.is_dir() or pathutil.same_path(build_directory, project_root) or not pathutil.path_within(build_directory, project_root):
        raise errors.ExecutorError(
            "INVALID_SHARED_BUILD_DIRECTORY",
            "A shared coverage build directory must be an existing project child path",
            [str(build_directory), str(project_root)],
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
        "adapter": adapter,
        "buildDirectory": str(build_directory),
        "removedGeneratedEntries": removed,
    }


def execute_plan(args: argparse.Namespace) -> dict[str, Any]:
    plan = fileio.load_json(args.plan)
    if plan.get("schema") != PLAN_SCHEMA:
        raise errors.ExecutorError("INVALID_PLAN_SCHEMA", f"Expected {PLAN_SCHEMA}")
    fileio.verify_integrity(plan)
    if os.name == "nt" and plan.get("adapter") in {"CMAKE", "CMAKE_GCC", "MAKE_GCC"}:
        cover_config = pathutil.canonical(plan.get("coverConfig", ""))
        toolchains.parse_cover_wrapper_config(cover_config)
        if plan.get("coverConfigSha256") != pathutil.sha256_file(cover_config):
            raise errors.ExecutorError(
                "COVER_CONFIG_CHANGED",
                "The validated COVER GCC config changed after build-plan generation",
                [str(cover_config)],
            )
    if args.phase not in plan.get("commands", {}):
        raise errors.ExecutorError("INVALID_BUILD_PHASE", f"Plan has no phase {args.phase}")
    log_dir = pathutil.canonical(args.log_dir, False)
    log_dir.mkdir(parents=True, exist_ok=True)
    specs = plan["commands"][args.phase]
    specs = specs if isinstance(specs, list) else [specs]
    results: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []
    shared_build_reset = reset_shared_coverage_build_directory(plan) if args.phase == "configure" else None
    for index, spec in enumerate(specs, start=1):
        suffix = f"-{index}" if len(specs) > 1 else ""
        coverage_spec = dict(spec)
        coverage_spec["coverToolEnvironment"] = True
        if args.phase in {"build", "rebuild"}:
            coverage_spec["instrumentationLanguage"] = "NATIVE"
        result = processes.run_command(coverage_spec, log_dir / f"{args.phase}{suffix}.log")
        if result["exitCode"] != 0:
            raise errors.ExecutorError("BUILD_COMMAND_FAILED", f"{args.phase} exited with {result['exitCode']}", [result["log"]])
        results.append(result)
    return envelopes.envelope(
        "EXECUTING_BUILD_" + args.phase.upper(),
        "SUCCEEDED",
        {
            "results": results,
            "verifications": verifications,
            "sharedBuildDirectoryReset": shared_build_reset,
        },
    )
