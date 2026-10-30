"""Isolated artifact collection and config-only preparation for local COVER PA."""

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
PATH_KEY = re.compile(r"(?:path|filename|directory)$|^(?:file|logfile)$", re.I)
PATH_OPTION = re.compile(r"(?:^|_)(?:path|file|directory|dir|output|log)(?:$|_)", re.I)
ABS_IN_TEXT = re.compile(
    r"[A-Za-z]:[\\/]|\\\\[^\\]|(?:^|[=\s,@])/(?![A-Za-z]+(?:[:\s]|$))"
    r"|(?:^|\s)-(?:I|L|F|isystem|iquote|include|imacros|idirafter|iprefix|iwithprefix(?:before)?|isysroot)[\"']?/"
)


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


def _audit_paths(workspace: Path, conf: Path, compiler_id: int) -> dict[str, Any]:
    audit: dict[str, Any] = {"safe": False, "blockers": [], "rewritten_paths": []}
    anchors: list[tuple[str, Path]] = []

    def block(location: str, message: str) -> None:
        audit["blockers"].append(f"{location}: {message}")

    def remap(value: str, location: str) -> str:
        raw = value.strip().strip('"')
        path = _portable(raw)
        if not raw or "\x00" in raw or "<cs_delim>" in raw:
            block(location, "Unsupported path value.")
            return value
        target = None
        for old, new in anchors:
            try:
                relative = path.relative_to(type(path)(old))
                target = new.joinpath(*relative.parts)
                break
            except ValueError:
                continue
        if target is None and not path.is_absolute() and not path.drive and ".." not in path.parts:
            target = workspace.joinpath(*path.parts)
        if target is None:
            block(location, f"No verified mapping for {value}")
            return value
        try:
            mapped = str(require_contained(target, workspace))
        except RecoveryError:
            block(location, f"Path escapes the verification workspace: {value}")
            return value
        if value != mapped:
            audit["rewritten_paths"].append({"location": location, "old": value, "new": mapped})
        return mapped

    with (
        contextlib.closing(sqlite3.connect(workspace / "global.db")) as global_db,
        contextlib.closing(sqlite3.connect(workspace / "project.db")) as project,
        global_db, project,
    ):
        trigger_definitions: dict[int, list[str]] = {}
        for db, label in ((global_db, "global.db"), (project, "project.db")):
            db.execute("PRAGMA trusted_schema=OFF")
            definitions = db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL ORDER BY name"
            ).fetchall()
            trigger_definitions[id(db)] = [sql for _, sql in definitions]
            for name, _ in definitions:
                quoted_trigger = '"' + name.replace('"', '""') + '"'
                db.execute(f"DROP TRIGGER {quoted_trigger}")
        old_conf = global_db.execute("SELECT confFile FROM Compiler WHERE idCompiler=?", (compiler_id,)).fetchone()[0]
        anchors.append((str(_portable(old_conf).parent), conf.parent))
        properties = {r[1] for r in project.execute('PRAGMA table_info("Properties")')}
        if {"name", "stringValue"}.issubset(properties):
            preprocess_roots = project.execute(
                "SELECT stringValue FROM Properties WHERE name=? AND stringValue IS NOT NULL", ("PREPROCESS_PATH",)
            ).fetchall()
            if len(preprocess_roots) > 1:
                block("Properties.PREPROCESS_PATH", "Multiple preprocessing roots cannot be mapped safely.")
            elif preprocess_roots and preprocess_roots[0][0]:
                anchors.append((preprocess_roots[0][0], workspace))
        anchors.append((str(workspace), workspace))

        for db, label in ((global_db, "global.db"), (project, "project.db")):
            # 가상 테이블과 내부 shadow table은 보존하되 경로 재작성 대상에서는 제외한다.
            # 일반 COVER 테이블만 복사본 안의 경로로 바꾼다.
            tables = [
                r[1] for r in db.execute("PRAGMA table_list")
                if r[0] == "main" and r[2] == "table" and not r[1].startswith("sqlite_")
            ]
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [r[1] for r in db.execute(f"PRAGMA table_info({quoted})")]
                cursor = db.execute(f"SELECT rowid,* FROM {quoted}")
                names = [item[0] for item in cursor.description]
                for values in cursor.fetchall():
                    row = dict(zip(names, values))
                    if table == "Compiler" and row.get("idCompiler") != compiler_id:
                        continue
                    for column in columns:
                        value = row[column]
                        if not isinstance(value, str) or not value:
                            continue
                        location = f"{label}:{table}[{values[0]}].{column}"
                        if table == "Compiler" and column == "confFile":
                            mapped = str(conf)
                            audit["rewritten_paths"].append({"location": location, "old": value, "new": mapped})
                        elif table == "Properties" and column == "stringValue" and row.get("name") == "PREPROCESS_PATH":
                            mapped = str(workspace)
                            audit["rewritten_paths"].append({"location": location, "old": value, "new": mapped})
                        elif PATH_KEY.search(column) or (
                            table == "Properties" and column == "stringValue" and PATH_OPTION.search(str(row.get("name", "")))
                        ):
                            mapped = remap(value, location)
                        elif column.lower() in ("command", "compileflag", "linkflag") and (
                            ABS_IN_TEXT.search(value) or re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", value)
                        ):
                            block(location, "Command text contains paths requiring manual review.")
                            continue
                        elif ABS_IN_TEXT.search(value) and column.lower() not in ("code", "message", "description", "label", "presentation"):
                            block(location, "Unclassified path-bearing value requires manual review.")
                            continue
                        else:
                            continue
                        if mapped != value:
                            field = '"' + column.replace('"', '""') + '"'
                            db.execute(f"UPDATE {quoted} SET {field}=? WHERE rowid=?", (mapped, values[0]))
        tu_columns = {r[1] for r in project.execute('PRAGMA table_info("TranslationUnit")')}
        project.execute("UPDATE TranslationUnit SET isSelected=CASE WHEN idCompiler=? THEN 1 ELSE 0 END", (compiler_id,))
        for field, value in (("isAnalyzed", 0), ("isChanged", 1)):
            if field in tu_columns:
                project.execute(f"UPDATE TranslationUnit SET {field}=? WHERE idCompiler=?", (value, compiler_id))
        # 복사본 수정 중에는 제품 트리거가 실행되지 않게 했지만, PA 입력 스키마는 원본과 같게 복원한다.
        for db in (global_db, project):
            for sql in trigger_definitions[id(db)]:
                db.execute(sql)

    for text_path in (conf, workspace / "pa.ini"):
        try:
            original, encoding, _ = read_preserved_text(text_path)
            rewritten = []
            for number, line in enumerate(original.splitlines(keepends=True), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", ";", "[")) or "=" not in line:
                    rewritten.append(line)
                    continue
                key, value = line.rstrip("\r\n").split("=", 1)
                location = f"{text_path.relative_to(workspace)}:{number}:{key.strip()}"
                if key.strip() == "cs_preinclude" or PATH_OPTION.search(key.strip()):
                    mapped = remap(value, location)
                    if key.strip() == "cs_preinclude" and not Path(mapped).is_file():
                        block(location, "Preinclude input is not present in the verification workspace.")
                    rewritten.append(key + "=" + mapped + line[len(line.rstrip("\r\n")):])
                else:
                    if ABS_IN_TEXT.search(value):
                        block(location, "Unclassified path-bearing option requires manual review.")
                    rewritten.append(line)
            changed = "".join(rewritten)
            if changed != original:
                text_path.write_bytes(encode_preserved_text(changed, encoding))
        except (OSError, RecoveryError, UnicodeError) as exc:
            block(str(text_path), f"Cannot audit text paths losslessly: {exc}")
    audit["safe"] = not audit["blockers"]
    return audit


def prepare_attempt(case: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
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
    try:
        audit = _audit_paths(workspace, conf, case["compiler_id"])
    except (OSError, sqlite3.Error, RecoveryError) as exc:
        audit = {"safe": False, "blockers": [f"Copied path audit is incomplete: {exc}"], "rewritten_paths": []}
    final_text, _, _ = read_preserved_text(conf)
    diff_path = attempt / "proposal.diff"
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), final_text.splitlines(keepends=True), fromfile="original.conf", tofile="candidate.conf"))
    with diff_path.open("x", encoding="utf-8", newline="") as output:
        output.write(diff)
    _check_originals(case["original_hashes"])
    if _hashes(input_root) != case["input_hashes"]:
        raise RecoveryError("InputChanged", "The preserved inputs changed during attempt preparation.")
    manifest = {
        "schema_version": 1, "compiler_id": case["compiler_id"], "selected_tu_ids": case["selected_tu_ids"],
        "expected_conf": str(conf), "original_hashes": case["original_hashes"],
        "workspace_hashes": _hashes(workspace), "path_audit": audit,
    }
    if (workspace / "verification.json").exists():
        raise RecoveryError("ManifestCollision", "Artifact already contains verification.json.")
    write_json(workspace / "verification.json", manifest)
    return {
        "workspace": str(workspace), "conf_path": str(conf), "conf_sha256": sha256_file(conf),
        "portable_conf_path": str(portable_conf), "portable_conf_sha256": sha256_file(portable_conf),
        "original_conf_path": str(source_conf), "original_conf_sha256": sha256_file(source_conf),
        "diff_path": str(diff_path), "compiler_id": case["compiler_id"], "selected_tu_ids": case["selected_tu_ids"],
        "path_audit": audit, "proposal": candidate,
    }
