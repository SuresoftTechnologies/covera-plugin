"""Read COVER artifacts without modifying them or prescribing a recovery."""

from __future__ import annotations

import contextlib
import posixpath
import re
import sqlite3
from pathlib import Path
from typing import Any

from .common import RecoveryError, decode_bytes


ERROR_RE = re.compile(r"\b(?P<fatal>fatal\s+)?error(?:\s*#\d+)?\s*:\s*(?P<message>.*)", re.I)
LOCATION_RE = re.compile(r'^"?(?P<path>.+?)"?,\s*line\s+(?P<line>\d+)\s*:', re.I)
SUMMARY_RE = re.compile(r"\b(?P<count>\d+)\s+errors?\s+detected\b", re.I)
INTERNAL_RE = re.compile(r"\binternal\s+(?:compiler\s+)?error\b", re.I)


def read_preserved_text(path: Path) -> tuple[str, str, str]:
    """Decode text losslessly and report its newline style."""
    raw = path.read_bytes()
    text, encoding = decode_bytes(raw)
    if raw.startswith(b"\xfe\xff"):
        encoding = "utf-16-be-bom"
    elif raw.startswith(b"\xff\xfe"):
        encoding = "utf-16-le-bom"
    try:
        if encode_preserved_text(text, encoding) != raw:
            raise RecoveryError("LossyEncoding", f"Decoding would alter bytes: {path}")
    except UnicodeEncodeError as exc:
        raise RecoveryError("LossyEncoding", f"Cannot round-trip text: {path}") from exc
    endings = set(re.findall(r"\r\n|(?<!\r)\n|\r(?!\n)", text))
    newline = next(iter(endings)) if len(endings) == 1 else ("" if not endings else "mixed")
    return text, encoding, newline


def encode_preserved_text(text: str, encoding: str) -> bytes:
    if encoding in ("utf-16-le-bom", "utf-16-be-bom"):
        prefix = b"\xff\xfe" if encoding == "utf-16-le-bom" else b"\xfe\xff"
        return prefix + text.encode(encoding[:-4])
    return text.encode(encoding)


def readonly_db(path: Path) -> sqlite3.Connection:
    """Load a SQLite snapshot into memory so the input DB cannot be journaled."""
    if any(Path(str(path) + suffix).exists() and Path(str(path) + suffix).stat().st_size for suffix in ("-wal", "-journal")):
        raise RecoveryError("ActiveDatabase", f"Checkpoint the database before collecting it: {path}")
    raw = path.read_bytes()
    if not raw.startswith(b"SQLite format 3\x00"):
        raise RecoveryError("InvalidDatabase", f"Not a SQLite database: {path}")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(raw)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryError("InvalidDatabase", f"SQLite check failed: {path}")
        return db
    except Exception:
        db.close()
        raise


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def _require_columns(db: sqlite3.Connection, table: str, required: set[str]) -> None:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = required - _columns(db, table) if table in tables else required
    if missing:
        raise RecoveryError(
            "UnsupportedSchema", f"{table} schema is not supported.", {"missing": sorted(missing)}
        )


def normalize_source_path(value: str) -> str:
    windows = "\\" in value or bool(re.match(r"^[A-Za-z]:", value))
    value = posixpath.normpath(value.strip().replace("\\", "/"))
    return value.casefold() if windows else value


def inspect_case(case_root: Path, source_path: str) -> dict[str, Any]:
    """Inspect one directory containing the COVER project/global databases."""
    project_path, global_path = case_root / "project.db", case_root / "global.db"
    blockers: list[str] = []
    missing = [name for name in ("project.db", "global.db", "pa.ini") if not (case_root / name).is_file()]
    if missing:
        return {"matched": False, "blockers": [f"Missing required file: {name}" for name in missing]}

    try:
        with contextlib.closing(readonly_db(project_path)) as project, contextlib.closing(readonly_db(global_path)) as global_db:
            _require_columns(project, "SourceFile", {"idSourceFile", "path"})
            _require_columns(
                project,
                "TranslationUnit",
                {"idTranslationUnit", "primarySourceFile", "idCompiler", "isActive", "isSelected", "isExcluded"},
            )
            _require_columns(global_db, "Compiler", {"idCompiler", "confFile"})

            requested = normalize_source_path(source_path)
            rows = project.execute(
                """SELECT s.idSourceFile, s.path, t.idTranslationUnit, t.idCompiler,
                          t.isActive, t.isSelected, t.isExcluded
                     FROM SourceFile s JOIN TranslationUnit t
                       ON t.primarySourceFile=s.idSourceFile"""
            ).fetchall()
            matches = [row for row in rows if normalize_source_path(str(row[1])) == requested]
            if not matches:
                return {"matched": False, "blockers": [], "available_sources": sorted({str(r[1]) for r in rows})}

            compiler_ids = {row[3] for row in matches}
            if None in compiler_ids or len(compiler_ids) != 1:
                return {
                    "matched": True,
                    "blockers": ["Requested source maps to zero or multiple compiler IDs."],
                    "compiler_ids": sorted(str(v) for v in compiler_ids),
                }
            compiler_id = next(iter(compiler_ids))
            flag_column = "t.compileFlag" if "compileFlag" in _columns(project, "TranslationUnit") else "NULL"
            all_tus = project.execute(
                f"""SELECT t.idTranslationUnit,t.primarySourceFile,t.isActive,t.isSelected,t.isExcluded,s.path,{flag_column}
                     FROM TranslationUnit t JOIN SourceFile s ON s.idSourceFile=t.primarySourceFile
                    WHERE t.idCompiler=? ORDER BY t.idTranslationUnit""",
                (compiler_id,),
            ).fetchall()
            tu_count = project.execute("SELECT count(*) FROM TranslationUnit WHERE idCompiler=?", (compiler_id,)).fetchone()[0]
            if tu_count != len(all_tus):
                blockers.append("Compiler has translation units without a corresponding SourceFile row.")
            unsupported = [r[0] for r in all_tus if r[2] != 1 or r[4] not in (0, None)]
            if unsupported:
                blockers.append(f"Compiler has inactive or excluded translation units: {unsupported}")

            compiler_columns = _columns(global_db, "Compiler")
            fields = [
                field
                for field in ("idCompiler", "idToolChain", "compilerPath", "include", "language", "confFile")
                if field in compiler_columns
            ]
            compiler_rows = global_db.execute(
                f'SELECT {",".join(fields)} FROM Compiler WHERE idCompiler=?', (compiler_id,)
            ).fetchall()
            if len(compiler_rows) != 1:
                blockers.append("Compiler row is missing or duplicated.")
                compiler = None
            else:
                compiler = dict(zip(fields, compiler_rows[0]))

            return {
                "matched": True,
                "blockers": blockers,
                "compiler_id": compiler_id,
                "selected_tu_ids": [row[0] for row in all_tus],
                "primary_source_ids": [row[1] for row in all_tus],
                "translation_units": [
                    {
                        "id": r[0], "source_id": r[1], "active": r[2], "selected": r[3],
                        "excluded": r[4], "source_path": r[5], "compile_flag": r[6]
                    }
                    for r in all_tus
                ],
                "compiler": compiler,
            }
    except (OSError, sqlite3.Error, RecoveryError) as exc:
        if isinstance(exc, RecoveryError):
            message = exc.message
        else:
            message = str(exc)
        return {"matched": False, "blockers": [f"Cannot inspect databases: {message}"]}


def summarize_elogs(case_root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    first_error: dict[str, Any] | None = None
    total = fatal = internal = 0
    missing: list[str] = []
    for path in sorted(case_root.rglob("*.elog")):
        try:
            text, encoding, _ = read_preserved_text(path)
        except (OSError, RecoveryError) as exc:
            missing.append(f"Unreadable elog {path.relative_to(case_root)}: {exc}")
            continue
        diagnostics = []
        fatal_here = internal_here = 0
        for number, line in enumerate(text.splitlines(), 1):
            match = ERROR_RE.search(line)
            if match and not SUMMARY_RE.search(line):
                location = LOCATION_RE.search(line[: match.start()])
                item = {
                    "elog": path.relative_to(case_root).as_posix(),
                    "elog_line": number,
                    "source": location.group("path") if location else None,
                    "source_line": int(location.group("line")) if location else None,
                    "message": match.group("message").strip(),
                    "raw": line.strip(),
                }
                diagnostics.append(item)
                first_error = first_error or item
                fatal_here += int(bool(match.group("fatal")))
            internal_here += len(INTERNAL_RE.findall(line))
        reported = [int(m.group("count")) for m in SUMMARY_RE.finditer(text)]
        error_count = max([len(diagnostics), *reported])
        total += error_count
        fatal += fatal_here
        internal += internal_here
        files.append(
            {
                "path": path.relative_to(case_root).as_posix(), "encoding": encoding,
                "encoding_is_inferred": encoding in ("utf-8", "cp949"),
                "errors": error_count, "fatal_errors": fatal_here, "internal_errors": internal_here,
            }
        )
    if not files:
        missing.append("No readable .elog files found.")
    return {
        "files": files,
        "first_error": first_error,
        "total_errors": total,
        "fatal_errors": fatal,
        "internal_errors": internal,
        "missing": missing,
    }


def collect_evidence(case_root: Path, source_path: str, inspected: dict[str, Any]) -> dict[str, Any]:
    """Keep observed facts separate from open questions and suggestions."""
    confirmed = {
        "requested_source": source_path,
        "compiler": inspected.get("compiler"),
        "translation_units": inspected.get("translation_units", []),
        "elog": summarize_elogs(case_root),
    }
    missing = list(confirmed["elog"].pop("missing"))
    if not inspected.get("compiler"):
        missing.append("Compiler details were not confirmed.")
    return {"confirmed": confirmed, "suggestions": [], "missing": missing}
