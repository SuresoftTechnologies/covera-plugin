"""Preserve diagnostic artifacts and prepare configuration for server TMPA requests."""

from __future__ import annotations

import contextlib
import difflib
import os
import re
import shutil
import sqlite3
import stat
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .common import RecoveryError, require_contained, sha256_file, write_json
from .evidence import collect_evidence, encode_preserved_text, inspect_case, read_preserved_text, readonly_db


MAX_ZIP_BYTES = 2 * 1024**3
MAX_ZIP_MEMBERS = 100_000


def _reject_link(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RecoveryError("UnsafePath", f"Links and Windows reparse points are not supported: {path}")


def _files(root: Path) -> list[Path]:
    _reject_link(root)
    if root.is_file():
        return [root]
    result = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            _reject_link(path)
            if path.is_file():
                result.append(path)
            elif not path.is_dir():
                raise RecoveryError("UnsafePath", f"Unsupported filesystem entry: {path}")
    return sorted(result)


def _hashes(root: Path, absolute: bool = False) -> dict[str, str]:
    return {
        str(path.resolve()) if absolute else path.relative_to(root).as_posix(): sha256_file(path)
        for path in _files(root)
    }


def _check_originals(hashes: dict[str, str]) -> None:
    for name, expected in hashes.items():
        path = Path(name)
        if not path.is_file() or sha256_file(path) != expected:
            raise RecoveryError("OriginalChanged", f"Original artifact changed or disappeared: {path}")


def _safe_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_ZIP_BYTES:
            raise RecoveryError("ArchiveLimit", "ZIP exceeds the extraction size or member limit.")
        seen: set[str] = set()
        checked = []
        for member in members:
            name = member.filename.replace("\\", "/").rstrip("/")
            parts = name.split("/")
            mode = member.external_attr >> 16
            if (
                not name or name.startswith("/") or any(part in ("", ".", "..") for part in parts)
                or any(any(c in part for c in ':<>"|?*') or part.endswith((".", " ")) for part in parts)
                or any(ord(c) < 32 for c in member.orig_filename)
                or any(re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\.|$)", p, re.I) for p in parts)
                or stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                or member.flag_bits & 1
            ):
                raise RecoveryError("UnsafeArchive", f"Unsafe or unsupported ZIP member: {member.filename}")
            key = name.casefold()
            if key in seen:
                raise RecoveryError("UnsafeArchive", f"Duplicate ZIP member: {member.filename}")
            seen.add(key)
            checked.append((member, require_contained(destination.joinpath(*parts), destination)))
        destination.mkdir()
        for member, target in checked:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


def _portable(value: str) -> PureWindowsPath | PurePosixPath:
    return PureWindowsPath(value) if "\\" in value or re.match(r"^[A-Za-z]:", value) else PurePosixPath(value)


def _relative_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise RecoveryError("UnsafePath", "A nonempty relative artifact path is required.")
    path = _portable(value)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise RecoveryError("UnsafePath", "Stored artifact paths must remain relative to their root.")
    return Path(*path.parts)


def _find_conf(root: Path, reference: Any) -> Path | None:
    if not isinstance(reference, str) or not reference:
        return None
    name = _portable(reference).name
    matches = [p for p in root.rglob("*.conf") if p.name.casefold() == name.casefold()]
    return matches[0] if len(matches) == 1 else None


def start_case(artifact_path: Path, run_dir: Path, source_path: str) -> dict[str, Any]:
    artifact_path, run_dir = Path(artifact_path).absolute(), Path(run_dir).absolute()
    if not isinstance(source_path, str) or not source_path.strip():
        raise RecoveryError("MissingSource", "An exact SourceFile.path is required.")
    if run_dir.exists():
        raise RecoveryError("WorkspaceExists", "run_dir must not already exist.")
    if not artifact_path.exists():
        raise RecoveryError("MissingArtifact", "The supplied artifact does not exist.")
    if artifact_path.is_dir() and run_dir.resolve().is_relative_to(artifact_path.resolve()):
        raise RecoveryError("UnsafePath", "The run directory cannot be inside the input artifact.")
    originals = _hashes(artifact_path, absolute=True)
    if artifact_path.is_file() and not zipfile.is_zipfile(artifact_path):
        raise RecoveryError("UnsupportedArtifact", "Supply a directory or a ZIP archive.")
    run_dir.mkdir(parents=True)
    input_root = run_dir / "inputs"
    try:
        if artifact_path.is_dir():
            shutil.copytree(artifact_path, input_root)
        else:
            _safe_zip(artifact_path, input_root)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RecoveryError("ArtifactCopyFailed", f"Cannot safely collect the artifact: {exc}") from exc
    _check_originals(originals)
    roots = sorted({p.parent for p in input_root.rglob("project.db")})
    examined = [(root, inspect_case(root, source_path)) for root in roots]
    matched = [(root, result) for root, result in examined if result.get("matched")]
    case: dict[str, Any] = {
        "artifact_path": str(artifact_path.resolve()), "run_dir": str(run_dir.resolve()),
        "input_root": str(input_root.resolve()), "case_root": None, "source_path": source_path,
        "compiler_id": None, "selected_tu_ids": [], "conf_relative_path": None,
        "original_hashes": originals, "input_hashes": _hashes(input_root), "blockers": [],
    }
    if len(matched) != 1:
        case["blockers"] = [
            "Requested source matches multiple case directories." if matched else "No complete case matches the requested source."
        ]
        case["blockers"].extend(message for _, result in examined for message in result.get("blockers", []))
        case["evidence"] = {
            "confirmed": {"elog": collect_evidence(input_root, source_path, {})["confirmed"]["elog"],
                          "matching_case_roots": [str(r.relative_to(input_root)) for r, _ in matched]},
            "suggestions": [], "missing": list(case["blockers"]),
        }
        return case
    root, inspected = matched[0]
    case.update({key: inspected.get(key) for key in ("compiler_id", "selected_tu_ids")})
    case["selected_tu_ids"] = case["selected_tu_ids"] or []
    case["case_root"] = root.relative_to(input_root).as_posix()
    case["blockers"] = list(inspected.get("blockers", []))
    for other_root, _ in examined:
        if other_root != root and case["compiler_id"] is not None:
            try:
                with contextlib.closing(readonly_db(other_root / "project.db")) as db:
                    if db.execute("SELECT 1 FROM TranslationUnit WHERE idCompiler=? LIMIT 1", (case["compiler_id"],)).fetchone():
                        case["blockers"].append("Another case contains the same compiler; multi-case verification is required before publishing.")
            except (OSError, sqlite3.Error, RecoveryError):
                case["blockers"].append("Cannot rule out same-compiler translation units in another archived case.")
    conf = _find_conf(root, (inspected.get("compiler") or {}).get("confFile"))
    if conf is None:
        case["blockers"].append("Compiler.confFile does not identify one archived .conf file.")
    else:
        case["conf_relative_path"] = conf.relative_to(root).as_posix()
    case["evidence"] = collect_evidence(root, source_path, inspected)
    if conf:
        try:
            text, encoding, newline = read_preserved_text(conf)
            case["evidence"]["confirmed"]["configuration"] = {
                "path": case["conf_relative_path"], "encoding": encoding, "newline": repr(newline),
                "encoding_is_inferred": encoding in ("utf-8", "cp949"),
                "text": text,
            }
        except (OSError, RecoveryError) as exc:
            case["blockers"].append(f"Configuration cannot be read losslessly: {exc}")
    return case


def _apply_candidate(conf: Path, candidate: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(candidate, dict) or not isinstance(candidate.get("reason"), str) or not candidate["reason"].strip():
        raise RecoveryError("InvalidCandidate", "A nonempty reason is required.")
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(v, str) or not v.strip() for v in evidence):
        raise RecoveryError("InvalidCandidate", "A nonempty list of evidence references is required.")
    replacements, additions = candidate.get("replacements", []), candidate.get("append_lines", [])
    if not isinstance(replacements, list) or not isinstance(additions, list) or not (replacements or additions):
        raise RecoveryError("InvalidCandidate", "Supply replacements or append_lines.")
    original, encoding, newline = read_preserved_text(conf)
    if newline == "mixed":
        raise RecoveryError("MixedNewlines", "Resolve mixed configuration newlines manually.")
    lines = original.splitlines(keepends=True)
    plain = [line.rstrip("\r\n") for line in lines]
    changes = {}
    for item in replacements:
        if not isinstance(item, dict) or not isinstance(item.get("old"), str) or not isinstance(item.get("new"), str):
            raise RecoveryError("InvalidCandidate", "Each replacement needs string old/new fields.")
        old, new = item["old"], item["new"]
        if not old or any(c in old + new for c in "\r\n\x00") or plain.count(old) != 1 or old in changes:
            raise RecoveryError("AmbiguousReplacement", "old must match exactly one complete line, once.")
        changes[old] = new
    if any(not isinstance(line, str) or any(c in line for c in "\r\n\x00") for line in additions):
        raise RecoveryError("InvalidCandidate", "append_lines entries must be single lines.")
    result = "".join(
        (changes[body] + line[len(body):] if changes[body] else "") if body in changes else line
        for body, line in zip(plain, lines)
    )
    if additions:
        ending = newline or "\n"
        result += (ending if result and not result.endswith(("\n", "\r")) else "") + ending.join(additions)
        if original.endswith(("\n", "\r")):
            result += ending
    if result == original:
        raise RecoveryError("NoConfigChange", "The candidate does not change the configuration.")
    try:
        encode_preserved_text(result, encoding)
    except UnicodeEncodeError as exc:
        raise RecoveryError("UnsupportedEncoding", "The candidate cannot use the original encoding.") from exc
    return original, result, encoding


def prepare_attempt(case: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Create a diagnostic snapshot and proposed.conf; never rewrite DB/INI paths."""
    if case.get("blockers") or not case.get("selected_tu_ids") or case.get("compiler_id") is None or not case.get("conf_relative_path"):
        raise RecoveryError("IncompleteCase", "Resolve case blockers before preparing a PA attempt.", {"blockers": case.get("blockers", [])})
    _reject_link(Path(case["run_dir"]))
    run_dir = Path(case["run_dir"]).resolve()
    input_root = require_contained(case["input_root"], run_dir)
    if input_root != run_dir / "inputs":
        raise RecoveryError("UnsafePath", "input_root must be run_dir/inputs.")
    _check_originals(case["original_hashes"])
    if _hashes(input_root) != case["input_hashes"]:
        raise RecoveryError("InputChanged", "The preserved input snapshot has changed.")
    case_relative = _relative_path(case["case_root"])
    conf_relative = _relative_path(case["conf_relative_path"])
    source_root = require_contained(input_root / case_relative, input_root)
    source_conf = require_contained(source_root / conf_relative, source_root)
    original, changed, encoding = _apply_candidate(source_conf, candidate)
    inspected = inspect_case(source_root, case["source_path"])
    if inspected.get("blockers") or inspected.get("compiler_id") != case["compiler_id"] or inspected.get("selected_tu_ids") != case["selected_tu_ids"]:
        raise RecoveryError("CaseMismatch", "Stored case selection does not match the preserved databases.")
    if (run_dir / "attempts").exists():
        _reject_link(run_dir / "attempts")
    attempts = require_contained(run_dir / "attempts", run_dir)
    attempts.mkdir(exist_ok=True)
    existing = [int(p.name) for p in attempts.iterdir() if p.name.isdigit()]
    attempt = attempts / f"{max(existing, default=0) + 1:03d}"
    attempt.mkdir()
    portable_conf = attempt / "proposed.conf"
    with portable_conf.open("xb") as output:
        output.write(encode_preserved_text(changed, encoding))
    workspace = attempt / "workspace"
    shutil.copytree(source_root, workspace)
    conf = workspace / conf_relative
    conf.write_bytes(encode_preserved_text(changed, encoding))
    diff_path = attempt / "proposal.diff"
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), changed.splitlines(keepends=True), fromfile="original.conf", tofile="candidate.conf"))
    with diff_path.open("x", encoding="utf-8", newline="") as output:
        output.write(diff)
    _check_originals(case["original_hashes"])
    if _hashes(input_root) != case["input_hashes"]:
        raise RecoveryError("InputChanged", "The preserved inputs changed during attempt preparation.")
    manifest = {
        "schema_version": 1, "compiler_id": case["compiler_id"], "selected_tu_ids": case["selected_tu_ids"],
        "expected_conf": str(conf), "original_hashes": case["original_hashes"],
        "workspace_hashes": _hashes(workspace),
    }
    if (workspace / "verification.json").exists():
        raise RecoveryError("ManifestCollision", "Artifact already contains verification.json.")
    write_json(workspace / "verification.json", manifest)
    return {
        "workspace": str(workspace), "conf_path": str(conf), "conf_sha256": sha256_file(conf),
        "portable_conf_path": str(portable_conf), "portable_conf_sha256": sha256_file(portable_conf),
        "original_conf_path": str(source_conf), "original_conf_sha256": sha256_file(source_conf),
        "diff_path": str(diff_path), "compiler_id": case["compiler_id"], "selected_tu_ids": case["selected_tu_ids"],
        "proposal": candidate,
    }
