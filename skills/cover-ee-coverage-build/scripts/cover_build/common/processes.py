"""Typed command execution, plan execution, and artifact snapshots."""

from __future__ import annotations

import argparse
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import native_logs
from cover_build.common import pathutil
from cover_build.common import toolchains
from cover_build.core import errors
from cover_build.core.errors import WorkflowWaiting


PLAN_SCHEMA = "cover-build-plan/v1"


def default_parallelism() -> int:
    """Choose a conservative host-aware default unless the caller overrides it."""
    configured = os.environ.get("COVER_BUILD_PARALLEL")
    if configured:
        try:
            value = int(configured)
        except ValueError as exc:
            raise errors.ExecutorError("INVALID_BUILD_PARALLELISM", "COVER_BUILD_PARALLEL must be a positive integer") from exc
        if value < 1:
            raise errors.ExecutorError("INVALID_BUILD_PARALLELISM", "COVER_BUILD_PARALLEL must be a positive integer")
        return value
    return max(1, min(os.cpu_count() or 1, 8))


def command_spec(executable: Path, arguments: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    return {
        "executable": str(executable),
        "arguments": arguments,
        "workingDirectory": str(cwd),
        "environmentDelta": {},
        "timeoutSeconds": timeout,
    }


def prepend_cover_agent_library_path(environment: dict[str, str]) -> dict[str, str]:
    """Make the installed COVER Agent runtime libraries available to commands."""
    install_path = environment.get("CODESCROLL_TM_INSTALL_PATH") or os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    if not install_path:
        return environment
    agent_library_path = pathutil.canonical(Path(install_path).expanduser() / "lib", False)
    prefix = str(agent_library_path)
    existing = environment.get("LD_LIBRARY_PATH", "")
    existing_parts = [part for part in existing.split(os.pathsep) if part]
    if prefix not in existing_parts:
        environment["LD_LIBRARY_PATH"] = os.pathsep.join([prefix, *existing_parts])
    else:
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            [prefix, *(part for part in existing_parts if part != prefix)]
        )
    return environment


def iter_output_lines(source: str | Path) -> Iterable[str]:
    if isinstance(source, Path):
        with source.open("r", encoding="utf-8", errors="replace") as stream:
            yield from stream
        return
    yield from source.splitlines()


def output_contains(source: str | Path, pattern: str, flags: int = 0) -> bool:
    expression = re.compile(pattern, flags=flags)
    return any(expression.search(line) for line in iter_output_lines(source))


def read_output_tail(path: str | Path, limit: int = 256 * 1024) -> str:
    source = Path(path)
    with source.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        return stream.read().decode("utf-8", errors="replace")


def run_command(spec: dict[str, Any], log_path: Path) -> dict[str, Any]:
    executable = pathutil.canonical(spec["executable"])
    cwd = pathutil.canonical(spec["workingDirectory"])
    arguments = spec.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise errors.ExecutorError("INVALID_COMMAND_SPEC", "arguments must be a string array")
    output_mode = str(spec.get("outputMode", "CAPTURE")).upper()
    if output_mode not in {"CAPTURE", "INHERIT_TERMINAL", "NATIVE_CONSOLE"}:
        raise errors.ExecutorError(
            "INVALID_COMMAND_SPEC",
            "outputMode must be CAPTURE, INHERIT_TERMINAL, or NATIVE_CONSOLE",
        )
    native_console = output_mode == "NATIVE_CONSOLE"
    if native_console and os.name != "nt":
        raise errors.ExecutorError("NATIVE_CONSOLE_UNSUPPORTED", "NATIVE_CONSOLE is available only on Windows")
    if spec.get("environmentMode") == "replace":
        env = dict(spec.get("environment", {}))
    else:
        env = os.environ.copy()
    for key, value in spec.get("environmentDelta", {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise errors.ExecutorError("INVALID_COMMAND_SPEC", "environmentDelta must contain strings")
        env[key] = value
    if spec.get("coverToolEnvironment") is True:
        prepend_cover_agent_library_path(env)
    started_at = fileio.utc_now()
    started = time.monotonic()
    log_path = pathutil.canonical(log_path, False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    latest_log = log_path
    attempt = 1
    while True:
        candidate = latest_log.with_name(f"{latest_log.stem}.attempt-{attempt:03d}{latest_log.suffix}")
        try:
            with candidate.open("x", encoding="utf-8"):
                pass
            log_path = candidate
            break
        except FileExistsError:
            attempt += 1
    partial_path = log_path.with_name(log_path.name + ".partial")
    # Product tools may print their Java build key; redact before any disk write.
    secret_values = [env[name] for name in ("COVER_ACCESS_KEY",) if env.get(name)]
    config = env.get("COVER_JACOV_CONFIG")
    if config:
        try:
            java_key = json.loads(Path(config).read_text(encoding="utf-8-sig")).get("key")
            if isinstance(java_key, str) and java_key:
                secret_values.append(java_key)
        except (OSError, ValueError, AttributeError):
            pass

    def safe_line(line: str) -> str:
        for secret in secret_values:
            line = line.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)(\bKey\s*:\s*)[^,\s)]+", r"\1[REDACTED]", line)
    tail: deque[str] = deque(maxlen=200)
    inherited_terminal = output_mode == "INHERIT_TERMINAL"
    unpiped_output = inherited_terminal or native_console
    process_arguments: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "shell": False,
    }
    if unpiped_output:
        # C# Toolbox needs Windows Console.OutputEncoding while it creates
        # .cmd wrappers. Its Windows-only adapter requests a hidden native
        # console; other callers may inherit the current terminal instead.
        process_arguments["stdout"] = None
        process_arguments["stderr"] = None
        if native_console:
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_info.wShowWindow = subprocess.SW_HIDE
            process_arguments["startupinfo"] = startup_info
            process_arguments["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        process_arguments.update(
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": locale.getpreferredencoding(False),
                "errors": "replace",
            }
        )
    process = subprocess.Popen([str(executable), *arguments], **process_arguments)
    reader_error: list[BaseException] = []

    def consume() -> None:
        try:
            with partial_path.open("w", encoding="utf-8", newline="") as output:
                for line in process.stdout:
                    line = safe_line(line)
                    output.write(line)
                    output.flush()
                    tail.append(line)
        except BaseException as exc:  # preserve reader failures after process cleanup
            reader_error.append(exc)

    reader: threading.Thread | None = None
    if not unpiped_output:
        assert process.stdout is not None
        reader = threading.Thread(target=consume, name="cover-build-log-stream", daemon=True)
        reader.start()
    timeout = int(spec.get("timeoutSeconds", 3600))
    next_heartbeat = started + 15
    timed_out = False
    while process.poll() is None:
        now = time.monotonic()
        if now - started >= timeout:
            timed_out = True
            process.kill()
            break
        if now >= next_heartbeat:
            print(
                f"[COVER] command running: {executable.name} ({int(now - started)}s), log={partial_path}",
                file=sys.stderr,
                flush=True,
            )
            next_heartbeat = now + 15
        time.sleep(0.2)
    process.wait()
    if reader is not None:
        reader.join(timeout=10)
        assert process.stdout is not None
        if reader.is_alive():
            process.stdout.close()
            reader.join(timeout=2)
        elif not process.stdout.closed:
            process.stdout.close()
    if reader_error:
        raise errors.ExecutorError("BUILD_LOG_STREAM_FAILED", str(reader_error[0]), [str(partial_path)])
    if unpiped_output:
        if native_console:
            message = "[COVER] command ran in a hidden native Windows console; no pipe capture was used"
        else:
            message = "[COVER] command output was inherited by the current Agent terminal; no pipe capture was used"
        log_path.write_text(f"{message}; exitCode={process.returncode}.\n", encoding="utf-8")
    else:
        os.replace(partial_path, log_path)
    shutil.copyfile(log_path, latest_log)
    fileio.atomic_write_json(log_path.with_suffix(".metrics.json"), {
        "log": str(log_path), "phase": latest_log.stem, "attempt": attempt,
        "startedAt": started_at, "finishedAt": fileio.utc_now(),
        "durationMs": round((time.monotonic() - started) * 1000),
        "exitCode": process.returncode, "timedOut": timed_out,
    })
    if timed_out:
        raise errors.ExecutorError("BUILD_COMMAND_TIMEOUT", f"Command exceeded {timeout} seconds", [str(log_path)])
    finished_at = fileio.utc_now()
    return {
        "executable": str(executable),
        "arguments": arguments,
        "workingDirectory": str(cwd),
        "exitCode": process.returncode,
        "durationSeconds": round(time.monotonic() - started, 3),
        "startedAt": started_at,
        "finishedAt": finished_at,
        "log": str(log_path),
        "outputPath": str(log_path),
        "outputTail": "".join(tail),
        "outputMode": output_mode,
    }


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
        result = run_command(coverage_spec, log_dir / f"{args.phase}{suffix}.log")
        if result["exitCode"] != 0:
            raise errors.ExecutorError("BUILD_COMMAND_FAILED", f"{args.phase} exited with {result['exitCode']}", [result["log"]])
        # Real executions stream to the log. Unit/integration adapters that
        # provide an in-memory legacy result remain readable during migration.
        candidate_log = Path(result["log"])
        log_source: str | Path = candidate_log if candidate_log.is_file() else str(result.get("output") or "")
        if output_contains(
            log_source,
            r"\[COVER_WARN\s*\]\s*\{Compile:Preprocess\}.*preprocess error",
            flags=re.IGNORECASE,
        ):
            raise errors.ExecutorError(
                "COVER_PREPROCESS_FAILED",
                "COVER wrapper reported a preprocessing failure even though the native build exited successfully",
                [result["log"]],
            )
        adapter = plan.get("adapter")
        if adapter == "MSBUILD_VCXPROJ" and args.phase == "rebuild":
            verifications.append(native_logs.verify_msbuild_wrapper_text(plan, log_source))
        if (args.phase == "preflight" and adapter == "MAKE_GCC") or (
            args.phase in {"build", "rebuild"} and adapter in {"CMAKE", "CMAKE_GCC", "MAKE_GCC"}
        ):
            verifications.append(
                native_logs.verify_gcc_preflight_text(
                    plan,
                    log_source,
                    conservative_native_tokens=args.phase == "preflight",
                )
            )
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


def snapshot_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for raw in args.artifact:
        path = pathutil.canonical(raw, False)
        exists = path.is_file()
        stat = path.stat() if exists else None
        items.append(
            {
                "path": str(path),
                "exists": exists,
                "size": stat.st_size if stat else None,
                "modifiedNs": stat.st_mtime_ns if stat else None,
                "sha256": pathutil.sha256_file(path) if exists else None,
            }
        )
    result = {"capturedAt": fileio.utc_now(), "artifacts": items}
    if args.output:
        fileio.atomic_write_json(pathutil.canonical(args.output, False), result)
    return envelopes.envelope("SNAPSHOTTING_ARTIFACTS", "SUCCEEDED", result)


def verify_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    before = fileio.load_json(args.before)
    after = fileio.load_json(args.after)
    before_map = {item["path"]: item for item in before["artifacts"]}
    failures: list[str] = []
    verified: list[dict[str, Any]] = []
    for item in after["artifacts"]:
        previous = before_map.get(item["path"])
        fresh = bool(item["exists"] and (not previous or not previous["exists"] or item["sha256"] != previous["sha256"] or item["modifiedNs"] != previous["modifiedNs"]))
        verified.append({**item, "fresh": fresh})
        if not fresh:
            failures.append(item["path"])
    if failures:
        raise errors.ExecutorError("ARTIFACT_MISSING_OR_STALE", "Artifacts are missing or stale", failures)
    return envelopes.envelope("VERIFYING_ARTIFACTS", "SUCCEEDED", {"artifacts": verified})


def require_execution(execute: bool) -> None:
    if execute:
        return
    raise WorkflowWaiting(
        "ORIGINAL_BUILD_EXECUTION_REQUIRED",
        "The original rebuild-equivalent build must run before Local Agent discovery",
        {"execute": True},
        decision_owner="USER",
        purpose="MUTATION_PERMISSION",
    )


def _without_output(result: dict[str, Any]) -> dict[str, Any]:
    copied = dict(result)
    copied.pop("output", None)
    copied.pop("outputTail", None)
    return copied


def _run(spec: dict[str, Any], log_path: Path, failure_code: str, description: str) -> dict[str, Any]:
    result = run_command(spec, log_path)
    if result["exitCode"] != 0:
        raise errors.ExecutorError(failure_code, description, [result["log"]])
    return result


def _command_environment(spec: dict[str, Any]) -> dict[str, str]:
    """Recreate the case-insensitive environment used for one typed command."""
    environment = dict(os.environ)
    for key, value in (spec.get("environmentDelta") or {}).items():
        for existing in list(environment):
            if existing.casefold() == key.casefold():
                environment.pop(existing)
        environment[key] = value
    return environment
