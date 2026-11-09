"""Windows Toolbox commands and reversible C# configuration changes."""

from __future__ import annotations

import os
import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from cover_build.pipelines.managed_common import _command_spec
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors


def _file_snapshot(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "modifiedNs": stat.st_mtime_ns,
        "sha256": pathutil.sha256_file(path),
    }


def _wrapper_file_snapshot(path: Path) -> dict[str, Any]:
    snapshot = _file_snapshot(path)
    if path.suffix.casefold() in {".cmd", ".bat"}:
        prefix = path.read_bytes()[:3]
        snapshot["batchEncoding"] = {
            "utf8Bom": prefix == b"\xef\xbb\xbf",
            "prefixHex": prefix.hex("-").upper(),
        }
    return snapshot


def _toolbox_state(projects: list[Path]) -> dict[str, Any]:
    project_states: list[dict[str, Any]] = []
    for project in projects:
        text = project.read_text(encoding="utf-8-sig", errors="replace")
        wrapper_directory = project.parent / ".cs_wrapper"
        wrapper_files = (
            [_wrapper_file_snapshot(path) for path in sorted(wrapper_directory.rglob("*")) if path.is_file()]
            if wrapper_directory.is_dir()
            else []
        )
        backup = Path(str(project) + "_backup")
        markers = {
            "cscToolPath": "CscToolPath" in text and ".cs_wrapper" in text,
            "cscToolExe": "CscToolExe" in text,
            "orgCscPath": "OrgCscPath" in text,
            "coverReference": "CoverCSharpLibrary" in text,
            "wrapperDirectory": wrapper_directory.is_dir(),
        }
        cover_configured = any(
            markers[name]
            for name in ("cscToolPath", "orgCscPath", "coverReference", "wrapperDirectory")
        )
        project_states.append(
            {
                "project": _file_snapshot(project),
                "backup": _file_snapshot(backup) if backup.is_file() else None,
                "wrapperDirectory": str(wrapper_directory),
                "wrapperFiles": wrapper_files,
                "markers": markers,
                "configured": cover_configured,
                "toolboxMarkersPresent": any(markers.values()),
            }
        )
    return {
        "projects": project_states,
        "configuredProjects": [
            item["project"]["path"] for item in project_states if item["configured"]
        ],
    }


def _toolbox_command_spec(
    toolbox: Path, arguments: list[str], cwd: Path, *, coverage: bool,
) -> dict[str, Any]:
    specification = _command_spec(toolbox, arguments, cwd, coverage=coverage)
    # C# Toolbox is supported on Windows only. MakeCmdFile derives .cmd bytes
    # from Console.OutputEncoding, so it needs a native Windows console rather
    # than the Agent's UTF-8 redirected terminal stream.
    specification["outputMode"] = "NATIVE_CONSOLE"
    return specification


def _run_toolbox_command(specification: dict[str, Any], log: Path, failure_code: str) -> dict[str, Any]:
    """Run the Windows-only Toolbox in its hidden native console."""
    result = processes.run_command(specification, log)
    if result["exitCode"] != 0:
        raise errors.ExecutorError(failure_code, f"C# Toolbox exited with {result['exitCode']}", [result["log"]])
    result.pop("outputTail", None)
    return result


def _utf8_bom_wrappers(state: dict[str, Any]) -> list[str]:
    return [
        str(wrapper["path"])
        for project in state["projects"]
        for wrapper in project["wrapperFiles"]
        if wrapper.get("batchEncoding", {}).get("utf8Bom") is True
    ]


def _toolbox_executable() -> Path:
    install_root = os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    if not install_root:
        raise errors.ExecutorError(
            "AGENT_ROOT_MISSING",
            "CODESCROLL_TM_INSTALL_PATH is required to locate cover-toolbox-cli.exe",
        )
    return pathutil.canonical(
        Path(install_root) / "bin" / "cover-toolbox" / "cover-toolbox-cli.exe"
    )


def _toolbox_csharp_config_path() -> Path:
    install_root = os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    if not install_root:
        raise errors.ExecutorError(
            "AGENT_ROOT_MISSING",
            "CODESCROLL_TM_INSTALL_PATH is required to locate the Toolbox C# configuration",
        )
    return Path(install_root).resolve() / "bin" / "cover-toolbox" / "csharp-config.json"


def _file_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    return {"exists": True, **_file_snapshot(path)}


def _same_file_state(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("exists") != right.get("exists"):
        return False
    if not left.get("exists"):
        return True
    return all(left.get(key) == right.get(key) for key in ("size", "modifiedNs", "sha256"))


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _toolbox_csharp_version(csc: Path) -> str:
    normalized = str(csc).replace("/", "\\").casefold()
    for version, marker in (("vs2026", "\\microsoft visual studio\\18\\"), ("vs2022", "\\microsoft visual studio\\2022\\"), ("vs2019", "\\microsoft visual studio\\2019\\"), ("vs2017", "\\microsoft visual studio\\2017\\")):
        if marker in normalized:
            return version
    return "custom"


def _prepare_csharp_toolbox_config(job: dict[str, Any], csc: Path) -> dict[str, Any]:
    """Temporarily point Toolbox's C# manager at the observed compiler only."""
    path = _toolbox_csharp_config_path()
    before = _file_state(path)
    original_bytes = path.read_bytes() if before["exists"] else None
    if original_bytes is None:
        raise errors.ExecutorError(
            "CSHARP_TOOLBOX_CONFIG_MISSING",
            "Installed Toolbox csharp-config.json is required for C# instrumentation",
            [str(path)],
        )
    try:
        parsed = json.loads(original_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.ExecutorError(
            "CSHARP_TOOLBOX_CONFIG_INVALID",
            "Toolbox csharp-config.json is not a valid UTF-8 JSON object",
            [str(path)],
        ) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("csharpConfig"), dict):
        raise errors.ExecutorError(
            "CSHARP_TOOLBOX_CONFIG_INVALID",
            "Toolbox csharp-config.json must contain csharpConfig object",
            [str(path)],
        )
    config = dict(parsed)
    config["vsVersion"] = {"version": _toolbox_csharp_version(csc), "cscPath": str(csc)}
    backup = Path(job["directories"]["temporary"]) / "csharp-toolbox-config.original"
    if original_bytes is not None:
        _atomic_write_bytes(backup, original_bytes)
    fileio.atomic_write_json(path, config)
    return {
        "path": str(path),
        "backup": str(backup),
        "before": before,
        "applied": _file_state(path),
        "selectedCompiler": str(csc),
        "selectedVersion": config["vsVersion"]["version"],
    }


def _restore_csharp_toolbox_config(config_state: dict[str, Any]) -> dict[str, Any]:
    path = Path(config_state["path"])
    current = _file_state(path)
    if not _same_file_state(current, config_state["applied"]):
        return {
            "status": "CONCURRENT_CHANGE",
            "restored": False,
            "before": config_state["before"],
            "applied": config_state["applied"],
            "current": current,
        }
    before = config_state["before"]
    if before["exists"]:
        backup = Path(str(config_state["backup"]))
        if not backup.is_file():
            return {"status": "BACKUP_MISSING", "restored": False, "before": before, "current": current}
        _atomic_write_bytes(path, backup.read_bytes())
        modified = before.get("modifiedNs")
        if isinstance(modified, int):
            os.utime(path, ns=(modified, modified))
    else:
        path.unlink()
    after = _file_state(path)
    if not _same_file_state(before, after):
        return {"status": "RESTORE_MISMATCH", "restored": False, "before": before, "after": after}
    return {"status": "RESTORED", "restored": True, "before": before, "after": after}


def _disable_toolbox_if_configured(
    toolbox: Path,
    entry: Path,
    projects: list[Path],
    log: Path,
    *,
    run_command: Callable[[dict[str, Any], Path, str], dict[str, Any]] = _run_toolbox_command,
) -> dict[str, Any]:
    before = _toolbox_state(projects)
    if not before["configuredProjects"]:
        return {"executed": False, "before": before, "after": before}
    command = run_command(
        _toolbox_command_spec(toolbox, ["disable", str(entry)], entry.parent, coverage=False),
        log,
        "CSHARP_TOOLBOX_DISABLE_FAILED",
    )
    after = _toolbox_state(projects)
    remaining = [
        item["project"]["path"]
        for item in after["projects"]
        if item["toolboxMarkersPresent"]
    ]
    if remaining:
        raise errors.ExecutorError(
            "CSHARP_TOOLBOX_DISABLE_INCOMPLETE",
            "cover-toolbox-cli disable did not remove every C# coverage setting",
            [log, *remaining],
        )
    return {"executed": True, "command": command, "before": before, "after": after}
