"""Safe, evidence-gated execution of the COVER PA runtime bundled with the skill.

This module deliberately does not download PA and does not edit databases or
configuration.  A caller must prepare an isolated workspace and record that
preparation in ``workspace/verification.json`` before calling :func:`run_pa`.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
import struct
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .common import RecoveryError, decode_bytes, sha256_file, utc_now


_RUN_DIRECTORY = ".pa-run"
_MANIFEST = "verification.json"
_MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
_RUNTIME_RESOURCE_SUFFIXES = {
    ".conf", ".dat", ".dll", ".h", ".ini", ".json", ".so", ".sqlt", ".xml",
}
_RUNTIME_RESOURCE_DIRECTORIES = ("bin", "config", "lib", "resources", "probe")
_FATAL_RE = re.compile(
    r"(?im)(?:fatal\s+error|internal\s+error|catastrophic\s+error|"
    r"aborted|stack\s+buffer\s+overrun|unhandled\s+exception)"
)
_PARSE_ERROR_RE = re.compile(
    r"(?im)(?:^|\s)(?:catastrophic\s+)?error(?:\s*#?\d+)?\s*:|"
    r"^\s*[^\r\n]+\berror\s+#\d+\b|^\s*\[(?:Error|Fatal)\]"
)
_ENVIRONMENT_RE = re.compile(
    r"(?im)(?:license[^\r\n]*(?:expired|invalid|missing|not found|failed|denied)|"
    r"(?:permission denied|access is denied)|no space left on device|"
    r"cannot open temporary file|cannot copy files|(?:dll|shared librar(?:y|ies))[^\r\n]*(?:missing|not found))"
)
_TU_COMMAND_RE = re.compile(
    r"--cs_set_tu_id=(\d+).*?--error_output=(?:\"([^\"]+)\"|(\S+))",
    re.IGNORECASE,
)
_REV_RE = re.compile(r"(?im)^\s*REV\s*:\s*(.+?)\s*$")


def _blocker(code: str, message: str, **details: Any) -> dict:
    item: dict[str, Any] = {"code": code, "message": message}
    if details:
        item["details"] = details
    return item


def _host_os() -> str:
    system = platform.system().lower()
    return "win" if system == "windows" else "linux" if system == "linux" else "unknown"


def bundled_runtime_root() -> Path:
    """Locate the deployed tmpa folder independently of the caller's cwd."""
    return Path(__file__).resolve().parents[2] / "assets/tmpa"


def resolve_runtime_paths(pa_root: str | Path | None = None,
                          executable: str | Path | None = None) -> tuple[Path, Path | None]:
    """Use an explicit PA selection or the skill bundle; normalize it for reuse."""
    root = Path(pa_root or executable or bundled_runtime_root()).expanduser().resolve()
    selected = Path(executable).expanduser() if executable is not None else None
    if selected is not None:
        if not selected.is_absolute() and pa_root is not None and not root.is_file():
            selected = root / selected
        selected = selected.resolve()
    elif root.is_file():
        selected = root
    return root, selected


def _binary_metadata(path: Path) -> dict:
    """Read only standard PE/ELF identity fields; do not execute the binary."""
    result = {"format": "Unknown", "os": "unknown", "architecture": "Unknown"}
    try:
        with path.open("rb") as stream:
            header = stream.read(4096)
    except OSError:
        return result
    if header.startswith(b"MZ") and len(header) >= 64:
        try:
            offset = struct.unpack_from("<I", header, 0x3C)[0]
            if offset + 6 <= len(header) and header[offset : offset + 4] == b"PE\0\0":
                machine = struct.unpack_from("<H", header, offset + 4)[0]
                result.update(
                    format="PE",
                    os="win",
                    architecture={0x014C: "x86", 0x8664: "x86_64", 0xAA64: "arm64"}.get(machine, f"0x{machine:04x}"),
                )
        except (IndexError, struct.error):
            pass
    elif header.startswith(b"\x7fELF") and len(header) >= 20:
        byte_order = "<" if header[5] == 1 else ">" if header[5] == 2 else ""
        if byte_order:
            machine = struct.unpack_from(byte_order + "H", header, 18)[0]
            bits = {1: "32", 2: "64"}.get(header[4], "?")
            architecture = {3: "x86", 62: "x86_64", 183: "arm64"}.get(machine, f"machine-{machine}")
            result.update(format=f"ELF{bits}", os="linux", architecture=architecture)
    return result


def _candidate_executables(root: Path) -> list[Path]:
    direct = [root / "tmpa", root / "tmpa.exe", root / "bin" / "tmpa", root / "bin" / "tmpa.exe"]
    found = [path for path in direct if path.is_file()]
    if found:
        return sorted({path.resolve() for path in found}, key=lambda item: str(item).casefold())
    skipped = {"error", "errors", "log", "logs", "sample", "samples", "src", "workspace", "workspaces"}
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name.casefold() not in skipped]
        for name in names:
            if name.casefold() in {"tmpa", "tmpa.exe"}:
                found.append((Path(current) / name).resolve())
        if len(found) > 16:
            break
    return sorted(set(found), key=lambda item: str(item).casefold())


def _runtime_files(root: Path, executable: Path) -> tuple[list[dict], list[dict]]:
    paths = {executable}
    for directory_name in _RUNTIME_RESOURCE_DIRECTORIES:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.casefold() in _RUNTIME_RESOURCE_SUFFIXES:
                paths.add(path.resolve())
                if len(paths) > 4096:
                    return [], [_blocker("RuntimeInventoryTooLarge", "Runtime resource inventory exceeds 4096 files.")]
    for path in executable.parent.iterdir():
        if path.is_file() and path.suffix.casefold() in _RUNTIME_RESOURCE_SUFFIXES:
            paths.add(path.resolve())
    result = []
    blockers = []
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        try:
            if not path.is_relative_to(root):
                blockers.append(_blocker("RuntimePathEscape", "A runtime resource leaves the runtime root.", path=str(path)))
                continue
            stat = path.stat()
            result.append(
                {
                    "path": str(path),
                    "relative_path": str(path.relative_to(root)),
                    "size": stat.st_size,
                    "sha256": sha256_file(path),
                    "role": "executable" if path == executable else "resource",
                }
            )
        except OSError as exc:
            blockers.append(_blocker("UnreadableRuntimeResource", "Cannot fingerprint a runtime resource.", path=str(path), error=str(exc)))
    return result, blockers


def _version_evidence(root: Path) -> list[dict]:
    evidence = []
    for name in ("VERSION", "version.txt", "README", "README.md"):
        path = root / name
        if not path.is_file() or path.stat().st_size > 128 * 1024:
            continue
        try:
            text, encoding = decode_bytes(path.read_bytes())
        except (OSError, RecoveryError):
            continue
        evidence.append(
            {
                "source": str(path),
                "encoding": encoding,
                "text": text[:1000],
                "verified_against_banner": False,
            }
        )
    return evidence


def inspect_runtime(pa_root: str | Path | None = None, executable: str | Path | None = None) -> dict:
    """Inspect the bundled tmpa folder, or an explicitly selected local PA."""
    blockers: list[dict] = []
    supplied_root, executable = resolve_runtime_paths(pa_root, executable)
    try:
        supplied_path = supplied_root.resolve(strict=True)
    except OSError as exc:
        return {
            "available": False,
            "root": str(supplied_root),
            "executable": "",
            "host_os": _host_os(),
            "host_architecture": platform.machine() or "Unknown",
            "version": "Unknown",
            "version_evidence": [],
            "files": [],
            "blockers": [_blocker("RuntimeNotFound", "PA root does not exist.", error=str(exc))],
        }
    standalone = supplied_path.is_file()
    root = supplied_path.parent if standalone else supplied_path
    if not root.is_dir():
        blockers.append(_blocker("InvalidRuntimeRoot", "PA root must be a directory or a tmpa executable."))
    selected: Path | None = None
    if standalone:
        selected = supplied_path
        if executable is not None and Path(executable).expanduser().resolve() != selected:
            blockers.append(_blocker("ExecutableMismatch", "A standalone tmpa path conflicts with --pa-executable."))
    elif executable is not None:
        value = Path(executable).expanduser()
        value = value if value.is_absolute() else root / value
        try:
            selected = value.resolve(strict=True)
        except OSError as exc:
            blockers.append(_blocker("ExecutableNotFound", "The supplied tmpa executable does not exist.", error=str(exc)))
        if selected is not None and not selected.is_relative_to(root):
            blockers.append(_blocker("RuntimePathEscape", "The supplied executable leaves the PA root."))
    elif root.is_dir():
        candidates = _candidate_executables(root)
        if len(candidates) == 1:
            selected = candidates[0]
        elif not candidates:
            blockers.append(_blocker("ExecutableNotFound", "No tmpa executable was found in the PA root."))
        else:
            blockers.append(
                _blocker("AmbiguousExecutable", "Multiple tmpa executables were found; select one explicitly.", candidates=[str(item) for item in candidates])
            )
    identity = _binary_metadata(selected) if selected is not None and selected.is_file() else {
        "format": "Unknown", "os": "unknown", "architecture": "Unknown"
    }
    host_os = _host_os()
    if selected is not None and not selected.is_file():
        blockers.append(_blocker("InvalidExecutable", "The selected tmpa path is not a file."))
    if selected is not None and identity["format"] == "Unknown":
        blockers.append(_blocker("UnsupportedExecutable", "tmpa is not a recognized PE or ELF executable."))
    if identity["os"] not in {"unknown", host_os}:
        blockers.append(_blocker("RuntimeOsMismatch", "tmpa binary OS contradicts the current host.", binary_os=identity["os"], host_os=host_os))
    if selected is not None and host_os == "linux" and not os.access(selected, os.X_OK):
        blockers.append(_blocker("ExecutablePermission", "tmpa is not executable by the current user."))
    files: list[dict] = []
    if selected is not None and selected.is_file() and selected.is_relative_to(root):
        if standalone:
            files = [{
                "path": str(selected), "relative_path": selected.name,
                "size": selected.stat().st_size, "sha256": sha256_file(selected),
                "role": "executable",
            }]
            inventory_blockers = []
        else:
            files, inventory_blockers = _runtime_files(root, selected)
        blockers.extend(inventory_blockers)
    return {
        "available": selected is not None and selected.is_file() and not blockers,
        "root": str(root),
        "executable": str(selected) if selected is not None else "",
        "host_os": host_os,
        "host_architecture": platform.machine() or "Unknown",
        "binary": identity,
        "version": "Unknown",
        "version_evidence": _version_evidence(root) if root.is_dir() else [],
        "files": files,
        "runtime_fingerprint": next((item["sha256"] for item in files if item["role"] == "executable"), ""),
        "standalone": standalone,
        "resource_validation": (
            "Standalone executable fingerprint only; dynamic loader and license availability are verified at execution."
            if standalone else "Fingerprint only; dynamic loader and license availability are verified at execution."
        ),
        "blockers": blockers,
    }


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _workspace_tree_blockers(workspace: Path) -> list[dict]:
    blockers = []
    for current, directories, names in os.walk(workspace, followlinks=False):
        for name in [*directories, *names]:
            path = Path(current) / name
            is_junction = getattr(path, "is_junction", lambda: False)()
            if path.is_symlink() or is_junction or not path.resolve().is_relative_to(workspace):
                blockers.append(_blocker("LinkedWorkspacePath", "Workspace contains a symlink, junction, or escaping path.", path=str(path)))
                if name in directories:
                    directories.remove(name)
    return blockers


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _manifest(workspace: Path, expected_conf: Path, compiler_id: int) -> tuple[dict | None, list[dict]]:
    blockers: list[dict] = []
    path = workspace / _MANIFEST
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, [_blocker("InvalidVerificationManifest", "workspace/verification.json is missing or invalid.", error=str(exc))]
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        blockers.append(_blocker("UnsupportedVerificationManifest", "verification.json schema_version must be 1."))
        return value if isinstance(value, dict) else None, blockers
    if value.get("compiler_id") != compiler_id:
        blockers.append(_blocker("CompilerMismatch", "Manifest compiler_id does not match the requested compiler."))
    try:
        recorded_conf = Path(value.get("expected_conf", "")).resolve(strict=True)
    except OSError:
        recorded_conf = None
    if recorded_conf != expected_conf:
        blockers.append(_blocker("ConfigMismatch", "Manifest expected_conf does not match the requested config copy."))
    selected = value.get("selected_tu_ids")
    if (
        not isinstance(selected, list)
        or not selected
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in selected)
        or len(set(selected)) != len(selected)
    ):
        blockers.append(_blocker("InvalidSelectedTus", "Manifest selected_tu_ids must be unique positive integers."))
    audit = value.get("path_audit")
    if not isinstance(audit, dict) or audit.get("safe") is not True or audit.get("blockers"):
        blockers.append(_blocker("UnsafePreparedPaths", "Preparation path audit did not prove the copied paths safe.", path_audit=audit))
    hashes = value.get("workspace_hashes")
    if not isinstance(hashes, dict) or not hashes:
        blockers.append(_blocker("MissingWorkspaceHashes", "Manifest must bind copied input files to SHA-256 values."))
    else:
        bound_paths = {(workspace / str(relative)).resolve() for relative in hashes}
        for required in (workspace / "project.db", workspace / "global.db", workspace / "pa.ini", expected_conf):
            if required not in bound_paths:
                blockers.append(_blocker("UnboundPaInput", "A required PA input is not bound by the manifest.", path=str(required)))
        for relative, expected_hash in hashes.items():
            candidate = workspace / str(relative)
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(workspace) or not resolved.is_file():
                    raise OSError("path leaves workspace or is not a file")
                if resolved.stat().st_nlink != 1:
                    blockers.append(_blocker("LinkedWorkspaceInput", "A copied input has multiple hard links.", path=str(relative)))
                elif not isinstance(expected_hash, str) or sha256_file(resolved) != expected_hash.casefold():
                    blockers.append(_blocker("WorkspaceHashMismatch", "A copied input no longer matches its manifest hash.", path=str(relative)))
            except OSError as exc:
                blockers.append(_blocker("MissingWorkspaceInput", "A manifest input is missing or unsafe.", path=str(relative), error=str(exc)))
    return value, blockers


def _database_state(workspace: Path, expected_conf: Path, compiler_id: int, selected_ids: Iterable[int]) -> tuple[dict | None, list[dict]]:
    blockers: list[dict] = []
    project = workspace / "project.db"
    global_db = workspace / "global.db"
    state: dict[str, Any] = {"project_sha256": "", "global_sha256": "", "translation_units": {}}
    try:
        for path in (project, global_db):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(workspace) or not resolved.is_file() or resolved.stat().st_nlink != 1:
                raise OSError(f"unsafe database copy: {path.name}")
        state["project_sha256"] = sha256_file(project)
        state["global_sha256"] = sha256_file(global_db)
        with closing(_connect_read_only(project)) as db:
            required = {"idTranslationUnit", "primarySourceFile", "isActive", "isSelected", "isAnalyzed", "isExcluded", "idCompiler"}
            if not required.issubset(_columns(db, "TranslationUnit")):
                raise sqlite3.DatabaseError("unsupported TranslationUnit schema")
            if not {"idSourceFile", "path"}.issubset(_columns(db, "SourceFile")):
                raise sqlite3.DatabaseError("unsupported SourceFile schema")
            rows = db.execute(
                "SELECT t.idTranslationUnit,t.primarySourceFile,t.isActive,t.isSelected,t.isAnalyzed,t.isExcluded,t.idCompiler,s.path "
                "FROM TranslationUnit t JOIN SourceFile s ON s.idSourceFile=t.primarySourceFile"
            ).fetchall()
            active = {int(row[0]) for row in rows if row[2] and row[3] and not row[5]}
            wanted = set(selected_ids)
            if active != wanted:
                blockers.append(_blocker("UnexpectedAnalysisScope", "Active selected TUs do not exactly match the manifest.", database=sorted(active), manifest=sorted(wanted)))
            for row in rows:
                tu_id = int(row[0])
                if tu_id not in wanted:
                    continue
                source = Path(row[7])
                source = source if source.is_absolute() else workspace / source
                resolved_source = source.resolve()
                if not resolved_source.is_relative_to(workspace):
                    blockers.append(_blocker("UnsafeSourcePath", "A selected TU points outside the verification workspace.", tu_id=tu_id, path=str(source)))
                elif not resolved_source.is_file():
                    preprocessed = workspace / "src" / "preprocessed"
                    expected_name = f"{source.stem}_{tu_id}.i{source.suffix}"
                    alternative = preprocessed / expected_name
                    if not alternative.is_file() or not alternative.resolve().is_relative_to(workspace):
                        blockers.append(_blocker("MissingCopiedSource", "Neither a copied source nor its known PA preprocessed input exists.", tu_id=tu_id, path=str(source), preprocessed=str(alternative)))
                if row[6] != compiler_id:
                    blockers.append(_blocker("CompilerMismatch", "A selected TU references another compiler.", tu_id=tu_id, database_compiler=row[6]))
                state["translation_units"][str(tu_id)] = {
                    "source": str(resolved_source),
                    "source_name": source.name,
                    "is_analyzed": bool(row[4]),
                }
        with closing(_connect_read_only(global_db)) as db:
            compiler_columns = _columns(db, "Compiler")
            if not {"idCompiler", "confFile"}.issubset(compiler_columns):
                raise sqlite3.DatabaseError("unsupported Compiler schema")
            rows = db.execute("SELECT confFile FROM Compiler WHERE idCompiler=?", (compiler_id,)).fetchall()
            if len(rows) != 1:
                blockers.append(_blocker("CompilerRowMismatch", "Exactly one Compiler row must match compiler_id.", count=len(rows)))
            else:
                configured = Path(rows[0][0])
                configured = (configured if configured.is_absolute() else workspace / configured).resolve()
                state["compiler_conf"] = str(configured)
                if configured != expected_conf or not configured.is_relative_to(workspace):
                    blockers.append(_blocker("ConfigMismatch", "Compiler.confFile does not reference the expected copied config.", actual=str(configured)))
                    state["compiler_conf_sha256"] = ""
                else:
                    state["compiler_conf_sha256"] = sha256_file(configured)
    except (OSError, sqlite3.Error, ValueError) as exc:
        blockers.append(_blocker("InvalidPaDatabase", "Cannot verify copied PA database inputs.", error=str(exc)))
        return None, blockers
    return state, blockers


def _instrumented_path(workspace: Path, source_name: str, tu_id: int) -> Path:
    source = Path(source_name)
    return workspace / "src" / "instrumented" / f"{source.stem}_{tu_id}{source.suffix}"


def _snapshot_outputs(workspace: Path, instrumented_paths: Iterable[Path]) -> dict[str, dict]:
    expected = set(instrumented_paths)
    result: dict[str, dict] = {}
    for directory in (workspace / "log", workspace / "obj", workspace / "src" / "instrumented"):
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.casefold() not in {".elog", ".log"} and path not in expected:
                continue
            stat = path.stat()
            result[str(path.resolve())] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": sha256_file(path)}
    return result


def _fresh_outputs(workspace: Path, before: dict[str, dict],
                   instrumented_paths: Iterable[Path]) -> tuple[list[dict], dict[str, bytes], list[dict]]:
    fresh = []
    text_bytes: dict[str, bytes] = {}
    blockers = []
    after = _snapshot_outputs(workspace, instrumented_paths)
    for raw_path, current in after.items():
        previous = before.get(raw_path)
        if previous == current:
            continue
        path = Path(raw_path)
        item = {"path": str(path.relative_to(workspace)), **current, "new": previous is None}
        fresh.append(item)
        if path.suffix.casefold() not in {".elog", ".log"}:
            continue
        if current["size"] > _MAX_EVIDENCE_BYTES:
            blockers.append(_blocker("EvidenceTooLarge", "A fresh log exceeds the safe evidence parsing limit.", path=item["path"], size=current["size"]))
            continue
        data = path.read_bytes()
        if previous and current["size"] >= previous["size"]:
            prefix = data[: previous["size"]]
            if hashlib.sha256(prefix).hexdigest() == previous["sha256"]:
                data = data[previous["size"] :]
        text_bytes[raw_path] = data
    fresh.sort(key=lambda item: item["path"].casefold())
    return fresh, text_bytes, blockers


def _decode_evidence(items: Iterable[tuple[str, bytes]]) -> tuple[str, list[dict]]:
    chunks = []
    blockers = []
    for name, data in items:
        try:
            text, _ = decode_bytes(data)
            chunks.append(f"\n--- {name} ---\n{text}")
        except RecoveryError:
            blockers.append(_blocker("UndecodableEvidence", "Fresh PA output cannot be decoded losslessly.", path=name))
    return "".join(chunks), blockers


def _read_capture(path: Path) -> tuple[bytes, list[dict]]:
    if path.stat().st_size > _MAX_EVIDENCE_BYTES:
        return b"", [_blocker("EvidenceTooLarge", "Captured PA output exceeds the parsing limit; the full file is preserved.", path=str(path))]
    return path.read_bytes(), []


def _kill_process_tree(process: subprocess.Popen[Any]) -> str:
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            completed = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                shell=False,
                check=False,
            )
            if completed.returncode != 0:
                process.kill()
                process.wait(timeout=10)
                return f"parent-killed-only;taskkill-exit-{completed.returncode}"
            process.wait(timeout=10)
            return "taskkill-exit-0"
        except (OSError, subprocess.SubprocessError):
            process.kill()
            process.wait(timeout=30)
            return "parent-killed-only"
    try:
        os.killpg(process.pid, 9)
        process.wait(timeout=10)
        return "process-group-killed"
    except (OSError, subprocess.SubprocessError):
        process.kill()
        process.wait(timeout=30)
        return "parent-killed-only"


def _result(status: str, blockers: list[dict], **values: Any) -> dict:
    return {"status": status, "blockers": blockers, **values}


def run_pa(
    workspace: Path,
    runtime: dict,
    expected_conf: Path,
    compiler_id: int,
    timeout_seconds: int = 600,
) -> dict:
    """Run tmpa in the selected copy and require multiple fresh success proofs."""
    started_at = utc_now()
    blockers: list[dict] = []
    try:
        workspace = Path(workspace).resolve(strict=True)
        expected_conf = Path(expected_conf).resolve(strict=True)
    except OSError as exc:
        return _result("VerificationBlocked", [_blocker("MissingVerificationInput", "Workspace or expected config does not exist.", error=str(exc))], started_at=started_at)
    if not workspace.is_dir() or not expected_conf.is_file() or not expected_conf.is_relative_to(workspace):
        blockers.append(_blocker("UnsafeVerificationInput", "Workspace must be a directory and expected_conf must be its file copy."))
    if isinstance(compiler_id, bool) or not isinstance(compiler_id, int) or compiler_id <= 0:
        blockers.append(_blocker("InvalidCompilerId", "compiler_id must be a positive integer."))
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        blockers.append(_blocker("InvalidTimeout", "timeout_seconds must be a positive integer."))
    if runtime.get("available") is not True or runtime.get("blockers"):
        blockers.append(_blocker("RuntimeUnavailable", "Runtime inspection did not produce an available, unblocked runtime.", runtime_blockers=runtime.get("blockers", [])))
    blockers.extend(_workspace_tree_blockers(workspace))
    executable = Path(str(runtime.get("executable", ""))).resolve()
    runtime_root = Path(str(runtime.get("root", ""))).resolve()
    if not executable.is_file() or not executable.is_relative_to(runtime_root):
        blockers.append(_blocker("UnsafeRuntime", "Runtime executable is missing or leaves its inspected root."))
    runtime_record = next((item for item in runtime.get("files", []) if item.get("role") == "executable"), None)
    if not runtime_record or runtime_record.get("sha256") != (sha256_file(executable) if executable.is_file() else ""):
        blockers.append(_blocker("RuntimeFingerprintMismatch", "tmpa changed after runtime inspection."))
    for item in runtime.get("files", []):
        resource = Path(str(item.get("path", ""))).resolve()
        try:
            if not resource.is_relative_to(runtime_root) or not resource.is_file():
                raise OSError("resource is missing or leaves runtime root")
            if sha256_file(resource) != item.get("sha256"):
                blockers.append(_blocker("RuntimeResourceChanged", "A runtime resource changed after inspection.", path=str(resource)))
        except OSError as exc:
            blockers.append(_blocker("MissingRuntimeResource", "A previously inspected runtime resource is unavailable.", path=str(resource), error=str(exc)))
    manifest, manifest_blockers = _manifest(workspace, expected_conf, compiler_id)
    blockers.extend(manifest_blockers)
    selected_ids = manifest.get("selected_tu_ids", []) if manifest else []
    if not isinstance(selected_ids, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in selected_ids):
        selected_ids = []
    before_db, database_blockers = _database_state(workspace, expected_conf, compiler_id, selected_ids)
    blockers.extend(database_blockers)
    pa_ini = workspace / "pa.ini"
    for required in (workspace / "project.db", workspace / "global.db", pa_ini):
        if not required.is_file() or not required.resolve().is_relative_to(workspace):
            blockers.append(_blocker("MissingPaInput", "A required copied PA input is missing.", path=str(required)))
    host_os = runtime.get("host_os")
    if host_os not in {"win", "linux"} or host_os != _host_os():
        blockers.append(_blocker("RuntimeOsMismatch", "Runtime host_os is missing or contradicts the current host."))
    run_directory = workspace / _RUN_DIRECTORY
    if run_directory.exists():
        blockers.append(_blocker("AttemptAlreadyUsed", "This verification workspace already contains PA run output."))
    if blockers:
        return _result(
            "VerificationBlocked",
            blockers,
            started_at=started_at,
            finished_at=utc_now(),
            command=[],
            workspace=str(workspace),
            runtime_fingerprint=runtime.get("runtime_fingerprint", ""),
            active_config={"path": str(expected_conf), "database_reference_verified": False, "runtime_load_trace": False},
        )

    command = [str(executable), str(workspace / "project.db"), str(workspace / "global.db"), host_os, str(pa_ini)]
    # Derive exact outputs from the selected TUs, preserving any source suffix.
    # A new extension must not need a separate whitelist to prove instrumentation.
    instrumented_paths = {
        _instrumented_path(workspace, before_db["translation_units"][str(tu_id)]["source_name"], tu_id)
        for tu_id in selected_ids
    }
    before_outputs = _snapshot_outputs(workspace, instrumented_paths)
    before_ini_hash = sha256_file(pa_ini)
    run_directory.mkdir()
    temp_directory = run_directory / "tmp"
    temp_directory.mkdir()
    stdout_path = run_directory / "stdout.log"
    stderr_path = run_directory / "stderr.log"
    env = os.environ.copy()
    env.update({"TEMP": str(temp_directory), "TMP": str(temp_directory), "TMPDIR": str(temp_directory)})
    if host_os == "linux":
        runtime_libraries = [str(path) for path in (executable.parent, runtime_root / "lib") if path.is_dir()]
        if runtime_libraries:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(runtime_libraries + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    creationflags = 0
    popen_options: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        popen_options = {"creationflags": creationflags}
    started = time.monotonic()
    timed_out = False
    termination = "not-needed"
    exit_code: int | None = None
    try:
        with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                **popen_options,
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                termination = _kill_process_tree(process)
                exit_code = process.returncode
    except OSError as exc:
        return _result(
            "VerificationBlocked",
            [_blocker("PaLaunchFailed", "tmpa could not be started.", error=str(exc))],
            started_at=started_at,
            finished_at=utc_now(),
            command=command,
            workspace=str(workspace),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            runtime_fingerprint=runtime.get("runtime_fingerprint", ""),
        )
    duration = round(time.monotonic() - started, 3)
    fresh_outputs, fresh_text_bytes, fresh_blockers = _fresh_outputs(workspace, before_outputs, instrumented_paths)
    blockers.extend(fresh_blockers)
    stdout_data, capture_blockers = _read_capture(stdout_path)
    blockers.extend(capture_blockers)
    stderr_data, capture_blockers = _read_capture(stderr_path)
    blockers.extend(capture_blockers)
    combined, decode_blockers = _decode_evidence(
        [(str(stdout_path), stdout_data), (str(stderr_path), stderr_data), *fresh_text_bytes.items()]
    )
    blockers.extend(decode_blockers)
    after_db, after_db_blockers = _database_state(workspace, expected_conf, compiler_id, selected_ids)
    blockers.extend(after_db_blockers)
    if after_db and before_db and after_db.get("compiler_conf_sha256") != before_db.get("compiler_conf_sha256"):
        blockers.append(_blocker("ConfigChangedDuringPa", "The selected config copy changed while tmpa was running."))
    if sha256_file(pa_ini) != before_ini_hash:
        blockers.append(_blocker("IniChangedDuringPa", "The selected pa.ini copy changed while tmpa was running."))

    version_match = _REV_RE.search(combined)
    version_evidence = {
        "version": version_match.group(1).strip() if version_match else "Unknown",
        "source": "fresh PA output" if version_match else "none",
        "verified": bool(version_match),
    }
    if version_match:
        banner = version_match.group(1)
        if ("Windows" in banner and host_os != "win") or ("Linux" in banner and host_os != "linux"):
            blockers.append(_blocker("BannerOsMismatch", "Fresh PA banner contradicts the inspected host OS.", banner=banner))
        expected_version = runtime.get("version", "Unknown")
        numeric_version = re.search(r"\bENT\s+(\d+\.\d+\.\d+)\b", banner)
        observed_versions = {banner, numeric_version.group(1) if numeric_version else ""}
        if expected_version != "Unknown" and expected_version not in observed_versions:
            blockers.append(_blocker("BannerVersionMismatch", "Fresh PA banner contradicts recorded version evidence.", banner=banner, expected_version=expected_version))
    environment_match = _ENVIRONMENT_RE.search(combined)
    fatal_match = _FATAL_RE.search(combined)
    parse_error_match = _PARSE_ERROR_RE.search(combined)
    fresh_elogs = [item for item in fresh_outputs if item["path"].casefold().endswith(".elog")]
    nonempty_elogs = [item for item in fresh_elogs if item["size"] > 0]
    current_elogs = [
        {"path": str(Path(raw_path).relative_to(workspace)), **item}
        for raw_path, item in _snapshot_outputs(workspace, instrumented_paths).items()
        if raw_path.casefold().endswith(".elog")
    ]
    current_nonempty_elogs = [item for item in current_elogs if item["size"] > 0]
    project_log, project_decode_blockers = _decode_evidence(
        (name, data)
        for name, data in fresh_text_bytes.items()
        if Path(name).name.casefold() in {"project_full.log", "analyzer.runtime.log"} and data
    )
    blockers.extend(project_decode_blockers)
    tu_commands = {int(match.group(1)): (match.group(2) or match.group(3)) for match in _TU_COMMAND_RE.finditer(project_log)}
    completion_markers = ["VALUE1", "VALUE34", "PHASE21002", "VALUE67", "VALUE101", "VALUE100"]
    console_text, console_decode_blockers = _decode_evidence([(str(stdout_path), stdout_data), (str(stderr_path), stderr_data)])
    blockers.extend(console_decode_blockers)
    marker_positions = {
        marker: (list(re.finditer(rf"(?m)^{re.escape(marker)}\s*$", console_text))[-1].start() if re.search(rf"(?m)^{re.escape(marker)}\s*$", console_text) else -1)
        for marker in completion_markers
    }
    completion = all(position >= 0 for position in marker_positions.values()) and all(
        marker_positions[left] < marker_positions[right]
        for left, right in zip(completion_markers, completion_markers[1:])
    )
    # This is useful context, but not a required success gate.  Some valid PA
    # builds finish without emitting this informational log line.
    project_end = "pa.main: end time:" in project_log
    tu_evidence: list[dict] = []
    persistent_selected_errors: list[str] = []
    for raw_id in selected_ids:
        before_tu = (before_db or {}).get("translation_units", {}).get(str(raw_id), {})
        after_tu = (after_db or {}).get("translation_units", {}).get(str(raw_id), {})
        error_path = tu_commands.get(raw_id, "")
        error_resolved = Path(error_path).resolve() if error_path else None
        error_inside = bool(error_resolved and error_resolved.is_relative_to(workspace))
        elog = next((item for item in fresh_elogs if error_resolved and (workspace / item["path"]).resolve() == error_resolved), None)
        elog_exists = bool(error_inside and error_resolved and error_resolved.is_file())
        elog_nonempty = bool(elog_exists and error_resolved and error_resolved.stat().st_size > 0)
        if elog_nonempty:
            persistent_selected_errors.append(str(error_resolved))
        elog_state = (
            "NonEmpty" if elog_nonempty
            else "EmptyFresh" if elog and elog["size"] == 0
            else "EmptyUnchanged" if elog_exists
            else "AbsentAfterRun"
        )
        source_name = before_tu.get("source_name", "")
        parsing = bool(source_name and re.search(rf"PHASE21001\|<cs#delim>\|\[\d+/\d+\]\s+{re.escape(source_name)}\(Parsing\)", console_text))
        end_parsing = bool(source_name and f"end parsing {source_name}" in project_log)
        fresh_instrumented = any(
            workspace / item["path"] == _instrumented_path(workspace, source_name, raw_id)
            for item in fresh_outputs
        )
        analyzed_transition = before_tu.get("is_analyzed") is False and after_tu.get("is_analyzed") is True
        error_output_clear = not error_path or (error_inside and not elog_nonempty)
        tu_evidence.append(
            {
                "tu_id": raw_id,
                "source_name": source_name,
                "parsing_progress": parsing,
                "end_parsing": end_parsing,
                "database_analyzed_transition": analyzed_transition,
                "fresh_instrumented_source": fresh_instrumented,
                "error_output": str(error_resolved) if error_resolved else "",
                "error_output_inside_workspace": error_inside,
                "error_output_clear": error_output_clear,
                "fresh_elog_empty": bool(elog and elog["size"] == 0),
                "elog_state": elog_state,
                "elog_nonempty": elog_nonempty,
            }
        )
    tus_confirmed = bool(tu_evidence) and all(
        item["parsing_progress"]
        and item["database_analyzed_transition"]
        and item["fresh_instrumented_source"]
        and item["error_output_clear"]
        and not item["elog_nonempty"]
        for item in tu_evidence
    )

    status = "VerificationBlocked"
    if timed_out:
        blockers.append(_blocker("PaTimeout", "tmpa exceeded the configured time limit.", tree_termination=termination))
    elif environment_match:
        blockers.append(_blocker("PaEnvironmentFailure", "Fresh PA output reports an environment failure.", evidence=environment_match.group(0)[:300]))
    elif nonempty_elogs or persistent_selected_errors or fatal_match or parse_error_match:
        status = "RecoveryFailed"
        evidence = (
            str(nonempty_elogs[0]["path"])
            if nonempty_elogs
            else persistent_selected_errors[0] if persistent_selected_errors
            else (fatal_match or parse_error_match).group(0)[:300]
        )
        blockers.append(_blocker("PaAnalysisFailure", "Fresh PA evidence contains a parsing, fatal, or internal error.", evidence=evidence))
    elif exit_code != 0:
        blockers.append(_blocker("UnclassifiedPaExit", "tmpa returned a non-zero status without a classified parsing or environment error.", exit_code=exit_code))
    elif blockers:
        pass
    elif not completion:
        blockers.append(_blocker("MissingCompletionEvidence", "Exit code 0 lacks the observed PA completion protocol.", markers=marker_positions, project_end=project_end))
    elif not tus_confirmed:
        blockers.append(_blocker("MissingTuEvidence", "One or more selected TUs lack fresh parsing, DB, instrumented-source, or cleared error-output proof."))
    else:
        status = "Recovered"

    return _result(
        status,
        blockers,
        started_at=started_at,
        finished_at=utc_now(),
        duration_seconds=duration,
        timeout_seconds=timeout_seconds,
        timed_out=timed_out,
        process_tree_termination=termination,
        command=command,
        command_shell=False,
        workspace=str(workspace),
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        runtime_fingerprint=runtime.get("runtime_fingerprint", ""),
        runtime_files=runtime.get("files", []),
        path_safety={"preparation_audit_verified": True, "os_sandbox_established": False},
        version_evidence=version_evidence,
        active_config={
            "path": str(expected_conf),
            "sha256": sha256_file(expected_conf),
            "compiler_id": compiler_id,
            "database_reference_verified": bool(after_db and after_db.get("compiler_conf") == str(expected_conf)),
            "runtime_load_trace": False,
        },
        analysis_evidence={
            "completion_protocol": "PA completion markers are checked together with fresh parsing, database progress, instrumented output, and cleared errors.",
            "completion_markers": marker_positions,
            "project_log_end": project_end,
            "selected_tus": tu_evidence,
            "fresh_outputs": fresh_outputs,
            "fresh_nonempty_elogs": nonempty_elogs,
            "current_nonempty_elogs": current_nonempty_elogs,
            "persistent_selected_errors": persistent_selected_errors,
            "fresh_fatal_or_internal": (fatal_match.group(0)[:300] if fatal_match else ""),
        },
        database_before=before_db,
        database_after=after_db,
    )
