"""Publish a verified coverage build as a project-local manual entrypoint."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import project_edit
from cover_build.core import errors
from cover_build.pipelines.java import persistent as java_persistent
from cover_build.pipelines.stm32cubeide import persistent as stm32_persistent
from cover_build.pipelines.visual_studio import persistent as vs_persistent


SCHEMA = "cover-ee-applied-setup/v1"


STATE_NAME = "applied-state.json"


COMMANDS_NAME = "coverage-commands.json"


SENSITIVE_NAME = re.compile(
    r"(?i)(?:password|passwd|token|secret|authorization|cookie|api[_-]?key|access[_-]?key)"
)


def root(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / ".cover-ee"


def state_path(project_root: str | Path) -> Path:
    return root(project_root) / STATE_NAME


def _reject_sensitive(value: Any, location: str = "value") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or SENSITIVE_NAME.search(key):
                raise errors.ExecutorError(
                    "SENSITIVE_PERSISTENT_SETUP",
                    "Persistent COVER setup cannot contain credential-like fields",
                    [f"{location}.{key}"],
                )
            _reject_sensitive(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive(child, f"{location}[{index}]")


def _asset_destination(config_directory: Path, source: Path) -> Path:
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:10]
    return config_directory / f"{digest}-{source.name}"


def _copy_assets(
    assets: Iterable[str | Path],
    config_directory: Path,
    *,
    stable_msbuild_props: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    records: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    msbuild_props_published = False
    for raw in assets:
        source = Path(raw).resolve(strict=True)
        if not source.is_file():
            raise errors.ExecutorError(
                "PERSISTENT_SETUP_ASSET_INVALID",
                "Only regular files can be promoted into the persistent COVER setup",
                [str(source)],
            )
        if stable_msbuild_props and source.suffix.casefold() in {".props", ".targets"}:
            if msbuild_props_published:
                raise errors.ExecutorError(
                    "PERSISTENT_MSBUILD_PROPS_AMBIGUOUS",
                    "Visual Studio persistence requires exactly one verified COVER property sheet",
                )
            destination = config_directory / "cover-msbuild.props"
            msbuild_props_published = True
        else:
            destination = _asset_destination(config_directory, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        replacements[str(source)] = str(destination)
        records.append(
            {
                "source": str(source),
                "path": str(destination),
                "size": destination.stat().st_size,
                "sha256": pathutil.sha256_file(destination),
            }
        )
    return records, replacements


def _portable_command(raw: dict[str, Any]) -> dict[str, Any]:
    required = ("executable", "arguments", "workingDirectory")
    if any(name not in raw for name in required):
        raise errors.ExecutorError(
            "PERSISTENT_SETUP_COMMAND_INVALID",
            "A persistent coverage command requires executable, arguments, and workingDirectory",
        )
    executable = Path(str(raw["executable"])).resolve(strict=True)
    working_directory = Path(str(raw["workingDirectory"])).resolve(strict=True)
    arguments = raw["arguments"]
    if not executable.is_file() or not working_directory.is_dir():
        raise errors.ExecutorError(
            "PERSISTENT_SETUP_COMMAND_INVALID",
            "Persistent coverage command paths must exist",
            [str(executable), str(working_directory)],
        )
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise errors.ExecutorError(
            "PERSISTENT_SETUP_COMMAND_INVALID",
            "Persistent coverage command arguments must be a string array",
        )
    environment_delta = dict(raw.get("environmentDelta") or {})
    if raw.get("environmentMode") == "replace":
        captured = raw.get("environment") or {}
        environment_delta.update(
            {
                key: value
                for key, value in captured.items()
                if isinstance(key, str)
                and isinstance(value, str)
                and (key.casefold() in vs_persistent.VS_ENVIRONMENT_NAMES or key.casefold().startswith("vscmd_arg_"))
            }
        )
    command = {
        "executable": str(executable),
        "arguments": list(arguments),
        "workingDirectory": str(working_directory),
        "environmentDelta": environment_delta,
        "timeoutSeconds": int(raw.get("timeoutSeconds", 3600)),
    }
    _reject_sensitive(command, "command")
    return command


def _runner_source() -> str:
    return '''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")

root = Path(__file__).resolve().parent
payload = json.loads((root / "coverage-commands.json").read_text(encoding="utf-8"))
log_root = root / "logs"
log_root.mkdir(parents=True, exist_ok=True)
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
for index, command in enumerate(payload["commands"], start=1):
    env = os.environ.copy()
    env.update(command.get("environmentDelta") or {})
    log = log_root / f"manual-{stamp}-{index:02d}.log"
    argv = [command["executable"], *command.get("arguments", [])]
    print("[COVER]", subprocess.list2cmdline(argv), flush=True)
    with log.open("w", encoding="utf-8", newline="") as output:
        process = subprocess.Popen(
            argv,
            cwd=command["workingDirectory"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                output.write(line)
            code = process.wait(timeout=command.get("timeoutSeconds", 3600))
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
    if code:
        print(f"[COVER] failed: exitCode={code}, log={log}", file=sys.stderr)
        raise SystemExit(code)
print(f"[COVER] coverage build completed; logs={log_root}")
'''


def _write_entrypoints(setup_root: Path) -> dict[str, str]:
    runner = setup_root / "run-coverage.py"
    runner.write_text(_runner_source(), encoding="utf-8", newline="\n")
    cmd = setup_root / "build-coverage.cmd"
    cmd.write_text(
        "@echo off\r\n"
        "where py >nul 2>nul\r\n"
        "if %errorlevel%==0 (\r\n"
        "  py -3 \"%~dp0run-coverage.py\" %*\r\n"
        ") else (\r\n"
        "  python \"%~dp0run-coverage.py\" %*\r\n"
        ")\r\n"
        "exit /b %errorlevel%\r\n",
        encoding="utf-8",
        newline="",
    )
    shell = setup_root / "build-coverage.sh"
    shell.write_text(
        '#!/usr/bin/env sh\nexec python3 "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/run-coverage.py" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    try:
        shell.chmod(shell.stat().st_mode | 0o111)
    except OSError:
        pass
    return {"windows": str(cmd), "posix": str(shell), "python": str(runner)}


def _apply_build_native_integration(
    setup_root: Path,
    build_system: str,
    commands: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    details: dict[str, Any],
) -> list[dict[str, Any]]:
    if build_system == "VISUAL_STUDIO":
        return vs_persistent._apply_msbuild(setup_root, assets, details)
    if build_system == "STM32CUBEIDE":
        changes: list[dict[str, Any]] = []
        try:
            changes.extend(stm32_persistent._apply_stm32_agent_config(setup_root, assets, details))
            changes.extend(stm32_persistent._apply_stm32_ide_environment(setup_root, details))
            return changes
        except BaseException:
            project_edit._restore_project_changes(changes)
            project_edit._remove_new_side_backups(changes)
            raise
    # CMake keeps the verified wrapper compiler selection in its normal cache;
    # C# Toolbox already owns the project mutation. Maven and Gradle are
    # intentionally command-driven, matching the COVER manual procedure.
    return []


def _adopt_existing_changes(
    setup_root: Path, details: dict[str, Any]
) -> list[dict[str, Any]]:
    adopted: list[dict[str, Any]] = []
    for raw in details.get("existingProjectChanges") or []:
        path = Path(raw["path"]).resolve(strict=True)
        backup = Path(raw["backupPath"]).resolve(strict=True)
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if current_hash.casefold() != str(raw.get("patchedSha256") or "").casefold():
            raise errors.ExecutorError(
                "PERSISTENT_SETUP_PATCH_MISMATCH",
                "The retained project patch no longer matches the verified coverage build",
                [str(path)],
            )
        identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        persistent_backup = setup_root / "backup" / f"{identity}-{path.name}.original"
        persistent_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, persistent_backup)
        adopted.append(
            {
                "kind": "RETAINED_VERIFIED_PATCH",
                "path": str(path),
                "originalBackup": str(persistent_backup),
                "originalSha256": raw["sha256"],
                "appliedSha256": raw["patchedSha256"],
            }
        )
    return adopted


def suspend(project_root: str | Path) -> dict[str, Any] | None:
    """Temporarily remove a prior project-file integration for an original build."""
    path = state_path(project_root)
    if not path.is_file():
        return None
    state = fileio.load_json(path)
    if state.get("schema") != SCHEMA or state.get("state") != "APPLIED":
        return None
    fileio.verify_integrity(state)
    suspended: list[dict[str, Any]] = []
    for change in state.get("projectChanges") or []:
        target = Path(change["path"])
        current = target.read_bytes()
        if hashlib.sha256(current).hexdigest() != change["appliedSha256"]:
            raise errors.ExecutorError(
                "PERSISTENT_SETUP_CONCURRENT_CHANGE",
                "A project file changed after COVER was applied; refusing to overwrite it",
                [str(target)],
            )
        applied_backup = root(project_root) / "backup" / f"resume-{hashlib.sha256(str(target).encode()).hexdigest()[:16]}.bin"
        applied_backup.write_bytes(current)
        if change.get("originalExisted", True):
            original = Path(change["originalBackup"]).read_bytes()
            target.write_bytes(original)
        else:
            target.unlink()
        suspended.append({**change, "appliedBackup": str(applied_backup)})
    if not suspended:
        return None
    updated = {key: value for key, value in state.items() if key != "integrity"}
    updated.update({"state": "SUSPENDED", "suspendedChanges": suspended})
    fileio.atomic_write_json(path, fileio.add_integrity(updated))
    return updated


def resume(project_root: str | Path, suspended: dict[str, Any] | None) -> None:
    """Restore the previously applied setup when a refresh fails or waits."""
    if not suspended:
        return
    for change in suspended.get("suspendedChanges") or []:
        target = Path(change["path"])
        if change.get("originalExisted", True):
            current = target.read_bytes()
            if hashlib.sha256(current).hexdigest() != change["originalSha256"]:
                raise errors.ExecutorError(
                    "PERSISTENT_SETUP_CONCURRENT_CHANGE",
                    "A project file changed while COVER setup was suspended",
                    [str(target)],
                )
        elif target.exists():
            raise errors.ExecutorError(
                "PERSISTENT_SETUP_CONCURRENT_CHANGE",
                "A project file appeared while COVER setup was suspended",
                [str(target)],
            )
        target.write_bytes(Path(change["appliedBackup"]).read_bytes())
    restored = {key: value for key, value in suspended.items() if key not in {"integrity", "suspendedChanges"}}
    restored["state"] = "APPLIED"
    fileio.atomic_write_json(state_path(project_root), fileio.add_integrity(restored))


def _publish_verified_setup(
    job: dict[str, Any],
    *,
    build_system: str,
    commands: list[dict[str, Any]],
    artifacts: list[str],
    assets: Iterable[str | Path] = (),
    mode: str = "PROJECT_LOCAL_LAUNCHER",
    details: dict[str, Any] | None = None,
    verify_applied: Callable[[], dict[str, Any]] | None = None,
    manual_changes: list | None = None,
) -> Path:
    """Publish only commands that already completed a verified coverage build."""
    if not commands:
        raise errors.ExecutorError(
            "PERSISTENT_SETUP_COMMAND_MISSING",
            "A successful coverage build must expose its reusable command sequence",
        )
    setup_root = root(job["projectRoot"])
    config_directory = setup_root / "config"
    config_directory.mkdir(parents=True, exist_ok=True)
    asset_records, replacements = _copy_assets(
        assets,
        config_directory,
        stable_msbuild_props=build_system == "VISUAL_STUDIO",
    )
    if build_system == "JAVA_GRADLE":
        manual_assets, manual_replacements = java_persistent._publish_gradle_manual_assets(
            asset_records,
            details or {},
            manual_changes,
        )
        asset_records.extend(manual_assets)
        replacements.update(manual_replacements)
    portable = [_portable_command(project_edit._replace_paths(command, replacements)) for command in commands]
    job_directory = str(Path(job["jobDirectory"]).resolve())
    if project_edit._contains_path_reference(portable, job_directory):
        raise errors.ExecutorError(
            "PERSISTENT_SETUP_REFERENCES_JOB",
            "Persistent coverage commands still reference the disposable job directory",
            [job_directory],
        )
    command_payload = fileio.add_integrity(
        {
            "schema": "cover-ee-manual-commands/v1",
            "projectRoot": str(Path(job["projectRoot"]).resolve()),
            "commands": portable,
        }
    )
    command_path = setup_root / COMMANDS_NAME
    fileio.atomic_write_json(command_path, command_payload)
    entrypoints = _write_entrypoints(setup_root)
    project_changes: list[dict[str, Any]] = []
    try:
        project_changes.extend(_adopt_existing_changes(setup_root, details or {}))
        project_changes.extend(_apply_build_native_integration(
            setup_root,
            build_system,
            portable,
            asset_records,
            details or {},
        ))
        structural_verification = (
            vs_persistent._verify_msbuild_integration(asset_records, details or {})
            if build_system == "VISUAL_STUDIO"
            else None
        )
        if build_system == "VISUAL_STUDIO" and verify_applied is None:
            raise errors.ExecutorError(
                "PERSISTENT_MSBUILD_VERIFICATION_MISSING",
                "Visual Studio persistence requires a Rebuild without command-line wrapper injection",
            )
        applied_verification = verify_applied() if verify_applied is not None else None
        if applied_verification is not None and not isinstance(applied_verification, dict):
            raise errors.ExecutorError(
                "PERSISTENT_SETUP_VERIFICATION_INVALID",
                "Persistent setup verification must return structured evidence",
            )
        if build_system == "VISUAL_STUDIO" and (
            not isinstance(applied_verification, dict)
            or applied_verification.get("verified") is not True
        ):
            raise errors.ExecutorError(
                "PERSISTENT_MSBUILD_VERIFICATION_FAILED",
                "The project-linked Visual Studio Rebuild was not verified",
            )
        staged = stm32_persistent._stm32_staged_config(details or {}) if build_system == "STM32CUBEIDE" else None
        if staged is not None:
            staged.unlink(missing_ok=True)
        public_details = {
            key: value
            for key, value in (details or {}).items()
            if key not in {"existingProjectChanges", "stagedAgentConfig"}
        }
        if structural_verification is not None:
            public_details["ideReuseVerification"] = {
                "projectIntegration": structural_verification,
                "rebuild": applied_verification,
            }
        elif applied_verification is not None:
            public_details["ideReuseVerification"] = applied_verification
        state = fileio.add_integrity(
            {
                "schema": SCHEMA,
                "state": "APPLIED",
                "sourceJobId": job["jobId"],
                "verifiedAt": fileio.utc_now(),
                "projectRoot": str(Path(job["projectRoot"]).resolve()),
                "buildSystem": build_system,
                "mode": mode,
                "commands": str(command_path),
                "entrypoints": entrypoints,
                "artifacts": [str(Path(path).resolve()) for path in artifacts],
                "assets": asset_records,
                "projectChanges": project_changes,
                "details": public_details,
                "requirements": [
                    "COVER Local Agent and the mapped toolchain must remain available",
                    "Run the project-local build-coverage entrypoint from a normal terminal",
                ],
            }
        )
        _reject_sensitive(state, "state")
        output = state_path(job["projectRoot"])
        fileio.atomic_write_json(output, state)
        return output
    except BaseException as error:
        try:
            project_edit._restore_project_changes(project_changes)
        except BaseException as restore_error:
            error.add_note(f"Persistent setup rollback failed: {restore_error}")
        try:
            project_edit._remove_new_side_backups(project_changes)
        except BaseException as cleanup_error:
            error.add_note(f"Persistent setup backup cleanup failed: {cleanup_error}")
        raise


def publish(
    job: dict[str, Any],
    *,
    build_system: str,
    commands: list[dict[str, Any]],
    artifacts: list[str],
    assets: Iterable[str | Path] = (),
    mode: str = "PROJECT_LOCAL_LAUNCHER",
    details: dict[str, Any] | None = None,
    verify_applied: Callable[[], dict[str, Any]] | None = None,
) -> Path:
    """Publish a verified setup and remove an owned temporary STM32 Agent config."""
    staged = stm32_persistent._stm32_staged_config(details or {}) if build_system == "STM32CUBEIDE" else None
    manual_changes: list = []
    try:
        return _publish_verified_setup(
            job,
            build_system=build_system,
            commands=commands,
            artifacts=artifacts,
            assets=assets,
            mode=mode,
            details=details,
            verify_applied=verify_applied,
            manual_changes=manual_changes,
        )
    except BaseException as error:
        for destination, previous, written_sha in reversed(manual_changes):
            if destination.is_file() and pathutil.sha256_file(destination).casefold() == written_sha.casefold():
                if previous is None:
                    destination.unlink()
                else:
                    temporary = destination.with_name(destination.name + ".cover-ee-tmp")
                    temporary.write_bytes(previous)
                    os.replace(temporary, destination)
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError as cleanup_error:
                error.add_note(f"STM32 staged-config cleanup failed: {cleanup_error}")
        raise
