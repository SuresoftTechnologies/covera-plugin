"""소스 경로가 DB에서 일치하는 로컬 오류 자료를 변경 없이 찾는다.

``complete``는 검색 단계의 최소 자료 확인 결과다. PA 실행 성공이나 서버
서버 반영 성공이 아니다. ZIP은 목록과 필요한 DB만 읽고 압축을 풀지 않는다.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator

from .common import RecoveryError
from .evidence import normalize_source_path, readonly_db
from .workspace import MAX_ZIP_BYTES, MAX_ZIP_MEMBERS


SKIP_DIRECTORIES = frozenset({".git", "node_modules", ".venv", "__pycache__"})
MAX_DB_BYTES = 64 * 1024**2
MAX_TOTAL_DB_BYTES = 256 * 1024**2
MAX_DB_ROWS = 100_000
MAX_ZIP_FILE_BYTES = 512 * 1024**2
MAX_ZIP_DIRECTORY_BYTES = 8 * 1024**2
MAX_COMPRESSION_RATIO = 1000
MAX_WARNINGS = 200
SIDECARS = ("-wal", "-journal", "-shm")
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\.|$)", re.I)


@dataclass
class _Budget:
    max_files: int
    warnings: list[str] = field(default_factory=list)
    entries: int = 0
    database_bytes: int = 0
    truncated: bool = False

    def warn(self, code: str, path: Path | str, message: str, *, limit: bool = False) -> None:
        self.truncated |= limit
        item = f"{code}: {path}: {message}"
        if item in self.warnings:
            return
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(item)
        elif self.warnings[-1] != "WarningLimit: 추가 경고는 생략됨.":
            self.warnings[-1] = "WarningLimit: 추가 경고는 생략됨."
            self.truncated = True

    def take(self, count: int, path: Path | str) -> bool:
        if count > self.max_files - self.entries:
            self.warn("SearchFileLimit", path, "파일·폴더·ZIP 항목 수 한도에 도달함.", limit=True)
            return False
        self.entries += count
        return True

    def database(self, size: int, path: Path | str) -> None:
        if size > MAX_DB_BYTES or size > MAX_TOTAL_DB_BYTES - self.database_bytes:
            self.truncated = True
            raise RecoveryError("DatabaseLimit", "DB 크기 또는 전체 DB 읽기 한도 초과.")
        self.database_bytes += size


@dataclass(frozen=True)
class _Member:
    name: str
    size: int
    value: Path | zipfile.ZipInfo


@dataclass
class _Index:
    path: Path
    kind: str
    files: dict[str, _Member]
    missing: list[str]
    modified_at: str | None
    archive: zipfile.ZipFile | None = None
    case_roots: list[str] | None = None

    def get(self, name: str) -> _Member | None:
        return self.files.get(name.casefold())


def _stamp(value: float) -> str | None:
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _plain(info: os.stat_result) -> bool:
    return not (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)


def _check_chain(path: Path) -> None:
    # resolve()나 is_file()보다 먼저 검사해 링크·UNC 경로를 따라가지 않는다.
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        raise RecoveryError("UnsafePath", "네트워크·장치 경로는 로컬 검색 대상이 아님.")
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if not _plain(info):
            raise RecoveryError("UnsafePath", "링크 또는 Windows reparse 경로는 검색하지 않음.")


def _run_folder(path: Path) -> bool:
    try:
        (path / "case.json").lstat()
        return True
    except FileNotFoundError:
        return False


def _scan_directory(root: Path, budget: _Budget, max_depth: int) -> tuple[dict[Path, os.stat_result], list[tuple[Path, str]], bool]:
    files: dict[Path, os.stat_result] = {}
    problems: list[tuple[Path, str]] = []
    pending = [(root, 0)]
    incomplete = False
    identities: dict[tuple[int, int], Path] = {}
    while pending:
        directory, depth = pending.pop()
        try:
            _check_chain(directory)
            if directory.name.casefold() in SKIP_DIRECTORIES or _run_folder(directory):
                problems.append((directory, "SkippedDirectory: 제외 폴더 또는 기존 복구 실행 폴더."))
                continue
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if not budget.take(1, root):
                        incomplete = True
                        return files, problems, incomplete
                    entries.append(entry)
            for entry in sorted(entries, key=lambda value: (value.name.casefold(), value.name)):
                path = directory / entry.name
                try:
                    # Windows DirEntry.stat는 inode/link 수를 0으로 줄 수 있다.
                    # 실제 lstat로 하드 링크와 중복 파일 검사 값을 확보한다.
                    info = path.lstat()
                    if not _plain(info):
                        raise RecoveryError("UnsafePath", "링크 또는 reparse 항목을 건너뜀.")
                    if stat.S_ISDIR(info.st_mode):
                        if entry.name.casefold() in SKIP_DIRECTORIES or _run_folder(path):
                            problems.append((path, "SkippedDirectory: 제외 폴더 또는 기존 복구 실행 폴더."))
                        elif depth >= max_depth:
                            budget.warn("SearchDepthLimit", path, "폴더 깊이 한도로 하위 항목을 확인하지 못함.", limit=True)
                            problems.append((path, "SearchDepthLimit: 하위 자료를 확인하지 못함."))
                        else:
                            pending.append((path, depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_nlink > 1:
                            raise RecoveryError("UnsafePath", "하드 링크 파일은 검색하지 않음.")
                        identity = (info.st_dev, info.st_ino)
                        if info.st_ino and identity in identities:
                            raise RecoveryError("DuplicatePath", "같은 파일을 가리키는 중복 항목.")
                        identities[identity] = path
                        files[path] = info
                    else:
                        raise RecoveryError("UnsafePath", "일반 파일·폴더가 아닌 항목.")
                except (OSError, RecoveryError) as exc:
                    code = exc.code if isinstance(exc, RecoveryError) else "UnreadablePath"
                    message = exc.message if isinstance(exc, RecoveryError) else "항목을 안전하게 읽을 수 없음."
                    budget.warn(code, path, message)
                    problems.append((path, f"{code}: {message}"))
        except (OSError, RecoveryError) as exc:
            code = exc.code if isinstance(exc, RecoveryError) else "UnreadablePath"
            budget.warn(code, directory, "폴더를 안전하게 검색할 수 없음.")
            problems.append((directory, f"{code}: 폴더 전체를 확인하지 못함."))
    return files, problems, incomplete


def _directory_indexes(root: Path, files: dict[Path, os.stat_result], problems: list[tuple[Path, str]], incomplete: bool) -> list[_Index]:
    cases = sorted(
        {path.parent for path in files if path.name.casefold() == "project.db"}
        | {path.parent for path, _ in problems if path.name.casefold() == "project.db"}
    )
    siblings: dict[Path, list[Path]] = {}
    for case in cases:
        siblings.setdefault(case.parent, []).append(case)
    boundaries = {case.parent if len(siblings[case.parent]) > 1 else case for case in cases}
    # 상위 case 또는 형제 case 묶음에 이미 들어간 하위 case를 따로 반환하지 않는다.
    containers = sorted(path for path in boundaries if not any(path != other and path.is_relative_to(other) for other in boundaries))
    result = []
    for container in containers:
        selected = {path: info for path, info in files.items() if path.is_relative_to(container)}
        missing = [message for path, message in problems if path.is_relative_to(container) or container.is_relative_to(path)]
        if incomplete:
            missing.append("SearchFileLimit: 검색 범위를 끝까지 확인하지 못함.")
        index: dict[str, _Member] = {}
        duplicates: set[str] = set()
        for path, info in selected.items():
            name = path.relative_to(container).as_posix()
            key = name.casefold()
            if key in index or key in duplicates:
                duplicates.add(key)
                index.pop(key, None)
                missing.append(f"DuplicatePath: 대소문자 구분 없이 중복되는 경로 {name}")
            else:
                index[key] = _Member(name, info.st_size, path)
        modified = max((item.st_mtime for item in selected.values()), default=0)
        result.append(_Index(container, "directory", index, missing, _stamp(modified), case_roots=[path.relative_to(container).as_posix() for path in cases if path.is_relative_to(container)]))
    return result


def _zip_header(path: Path, size: int, budget: _Budget) -> int:
    """ZipFile이 중앙 목록을 할당하기 전에 그 크기와 항목 수부터 제한한다."""
    if size > MAX_ZIP_FILE_BYTES:
        raise RecoveryError("ArchiveLimit", "ZIP 파일 크기 한도 초과.")
    with path.open("rb") as stream:
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or len(tail) - end < 22:
        raise RecoveryError("InvalidArchive", "정상 ZIP 종료 레코드를 찾지 못함.")
    _, disk, central_disk, disk_count, count, central_size, offset, comment = struct.unpack_from("<4s4H2LH", tail, end)
    if end + 22 + comment != len(tail) or disk or central_disk or disk_count != count:
        raise RecoveryError("UnsupportedArchive", "분할 ZIP 또는 비표준 ZIP 구조는 지원하지 않음.")
    if count == 0xFFFF or central_size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise RecoveryError("UnsupportedArchive", "ZIP64는 이 제한된 검색기에서 지원하지 않음.")
    if central_size > MAX_ZIP_DIRECTORY_BYTES or count > MAX_ZIP_MEMBERS:
        raise RecoveryError("ArchiveLimit", "ZIP 중앙 목록 크기 또는 항목 수 한도 초과.")
    if offset + central_size > size - len(tail) + end:
        raise RecoveryError("InvalidArchive", "ZIP 중앙 목록 범위가 잘못됨.")
    if not budget.take(count, path):
        raise RecoveryError("SearchFileLimit", "ZIP 항목을 모두 검사할 수 없어 열지 않음.")
    return count


def _zip_index(path: Path, archive: zipfile.ZipFile, max_depth: int, declared_count: int) -> _Index:
    members = archive.infolist()
    if len(members) != declared_count:
        raise RecoveryError("InvalidArchive", "ZIP 목록과 종료 레코드의 항목 수가 다름.")
    if len(members) > MAX_ZIP_MEMBERS or sum(member.file_size for member in members) > MAX_ZIP_BYTES:
        raise RecoveryError("ArchiveLimit", "ZIP 전체 해제 크기 또는 항목 수 한도 초과.")
    names: dict[str, tuple[str, bool]] = {}
    files: dict[str, _Member] = {}
    for member in members:
        name = member.filename.replace("\\", "/").rstrip("/")
        parts = name.split("/")
        mode = member.external_attr >> 16
        directory = member.is_dir()
        if (
            not name or name.startswith("/") or any(part in ("", ".", "..") for part in parts)
            or any(any(char in part for char in ':<>"|?*') or part.endswith((".", " ")) or _RESERVED.match(part) for part in parts)
            or any(ord(char) < 32 for char in member.orig_filename)
            or stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
            or member.external_attr & 0x400 or member.flag_bits & 1
            or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or bool(stat.S_ISDIR(mode)) and not directory
        ):
            raise RecoveryError("UnsafeArchive", f"위험하거나 지원하지 않는 ZIP 항목: {name}")
        if len(parts) - (0 if directory else 1) > max_depth:
            raise RecoveryError("ArchiveDepthLimit", "ZIP 내부 경로 깊이 한도 초과.")
        if member.file_size > MAX_COMPRESSION_RATIO * max(1, member.compress_size):
            raise RecoveryError("ArchiveLimit", "압축 비율 한도 초과; 압축 폭탄 가능성.")
        key = name.casefold()
        if key in names:
            raise RecoveryError("UnsafeArchive", f"중복 ZIP 경로: {name}")
        names[key] = (name, directory)
        if not directory:
            files[key] = _Member(name, member.file_size, member)
    for key, (name, _) in names.items():
        for parent in PurePosixPath(key).parents:
            if str(parent) in names and not names[str(parent)][1]:
                raise RecoveryError("UnsafeArchive", f"파일과 폴더가 충돌하는 ZIP 경로: {name}")
    skipped = {str(PurePosixPath(name).parent) for name in files if PurePosixPath(name).name == "case.json"}
    missing = []
    for key, member in list(files.items()):
        parents = {str(parent) for parent in PurePosixPath(key).parents}
        if any(part in SKIP_DIRECTORIES for part in PurePosixPath(key).parts[:-1]) or parents & skipped:
            del files[key]
            if not missing:
                missing.append("SkippedDirectory: ZIP에 제외 폴더 또는 기존 복구 실행 폴더가 포함됨.")
    return _Index(path, "zip", files, missing, _stamp(path.stat().st_mtime), archive)


@contextlib.contextmanager
def _database(index: _Index, member: _Member, budget: _Budget) -> Iterator[sqlite3.Connection]:
    if any(index.get(member.name + suffix) is not None for suffix in SIDECARS):
        raise RecoveryError("ActiveDatabase", "WAL·journal·SHM이 남은 DB는 일관된 수집본인지 확인이 필요함.")
    budget.database(member.size, f"{index.path}/{member.name}")
    db = None
    try:
        if isinstance(member.value, Path):
            _check_chain(member.value)
            info = member.value.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size != member.size:
                raise RecoveryError("ChangedArtifact", "검색 도중 DB 파일 상태가 달라짐.")
            for suffix in SIDECARS:
                try:
                    Path(str(member.value) + suffix).lstat()
                except FileNotFoundError:
                    continue
                raise RecoveryError("ActiveDatabase", "DB 옆의 WAL·journal·SHM을 먼저 확인해야 함.")
            # 원본을 SQLite 파일로 열지 않고 기존 helper로 메모리 snapshot을 연다.
            db = readonly_db(member.value)
        else:
            assert index.archive is not None
            with index.archive.open(member.value) as stream:
                raw = stream.read(MAX_DB_BYTES + 1)
            if len(raw) != member.size or len(raw) > MAX_DB_BYTES or not raw.startswith(b"SQLite format 3\x00"):
                raise RecoveryError("InvalidDatabase", "ZIP DB 크기 또는 SQLite 헤더가 잘못됨.")
            db = sqlite3.connect(":memory:")
            if not hasattr(db, "deserialize"):
                raise RecoveryError("UnsupportedSQLite", "메모리 DB deserialize를 지원하는 Python/SQLite가 필요함.")
            db.deserialize(raw)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
        operations = 0

        def stop_expensive_query() -> int:
            nonlocal operations
            operations += 1
            return int(operations > 5000)

        db.set_progress_handler(stop_expensive_query, 1000)
        if hasattr(db, "setlimit"):
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024**2)
            db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryError("InvalidDatabase", "SQLite 무결성 확인 실패.")
        yield db
    finally:
        if db is not None:
            db.close()


def _columns(db: sqlite3.Connection, table: str, required: set[str]) -> set[str]:
    # table은 이 모듈의 고정된 COVER 테이블명만 받는다.
    row = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
    columns = {item[1] for item in db.execute(f'PRAGMA table_info("{table}")')} if row == ("table",) else set()
    if not required.issubset(columns):
        raise RecoveryError("UnsupportedSchema", f"{table} 필수 열을 확인하지 못함: {', '.join(sorted(required - columns))}")
    return columns


def _rows(db: sqlite3.Connection, query: str, args: tuple = ()) -> list[tuple]:
    rows = db.execute(query, args).fetchmany(MAX_DB_ROWS + 1)
    if len(rows) > MAX_DB_ROWS:
        raise RecoveryError("DatabaseRowLimit", "DB 행 수 검사 한도 초과.")
    return rows


def _optional_rows(db: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> list[tuple]:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is None:
        return []
    _columns(db, table, set(columns))
    return _rows(db, f'SELECT {",".join(columns)} FROM "{table}"')


def _portable(value: str) -> PureWindowsPath | PurePosixPath:
    return PureWindowsPath(value) if "\\" in value or re.match(r"^[A-Za-z]:", value) else PurePosixPath(value)


def _join(root: str, relative: str | PurePosixPath) -> str:
    return (PurePosixPath(root) / relative).as_posix()


def _mapped(index: _Index, case: str, reference: Any, anchors: list[tuple[str, str]]) -> _Member | None:
    if not isinstance(reference, str) or not reference or "\x00" in reference:
        return None
    value = _portable(reference)
    if ".." in value.parts:
        return None
    choices: set[str] = set()
    if not value.is_absolute() and not value.drive:
        choices.add(_join(case, PurePosixPath(*value.parts)).casefold())
    for old, new in anchors:
        old_path = _portable(old)
        if type(old_path) is not type(value):
            continue
        try:
            relative = value.relative_to(old_path)
        except ValueError:
            continue
        choices.add(_join(new, PurePosixPath(*relative.parts)).casefold())
    members = [index.files[key] for key in choices if key in index.files]
    return members[0] if len(members) == 1 else None


def _record_failure(budget: _Budget, index: _Index, case: str, exc: Exception) -> str:
    code = exc.code if isinstance(exc, RecoveryError) else "InvalidDatabase"
    message = exc.message if isinstance(exc, RecoveryError) else "DB를 안전하게 읽거나 해석할 수 없음."
    budget.warn(code, f"{index.path}/{case}", message, limit=code.endswith("Limit"))
    return f"{code}: {message}"


def _inspect_case(index: _Index, case: str, requested: str, budget: _Budget) -> dict[str, Any] | None:
    project = index.get(_join(case, "project.db"))
    if project is None:
        return None
    missing: list[str] = []
    anchors: list[tuple[str, str]] = []
    sources: dict[int, str] = {}
    tus: list[tuple] = []
    logs: list[tuple] = []
    matching_ids: set[int] = set()
    matched = False
    try:
        with _database(index, project, budget) as db:
            _columns(db, "SourceFile", {"idSourceFile", "path"})
            source_rows = _rows(db, "SELECT idSourceFile,path FROM SourceFile")
            matching_rows = [row for row in source_rows if isinstance(row[1], str) and normalize_source_path(row[1]) == requested]
            if not matching_rows:
                return None
            matched = True
            matching_ids = {row[0] for row in matching_rows}
            sources = {row[0]: row[1] for row in source_rows}
            if len(sources) != len(source_rows) or len(matching_rows) != 1:
                missing.append("AmbiguousSource: SourceFile의 소스 경로 또는 ID가 중복됨.")
            _columns(db, "TranslationUnit", {"idTranslationUnit", "primarySourceFile", "idCompiler", "isActive", "isSelected", "isExcluded"})
            tus = _rows(db, "SELECT idTranslationUnit,primarySourceFile,idCompiler,isActive,isSelected,isExcluded FROM TranslationUnit")
            preprocess_roots = [value for name, value in _optional_rows(db, "Properties", ("name", "stringValue")) if name == "PREPROCESS_PATH" and isinstance(value, str) and value]
            if len(preprocess_roots) > 1:
                missing.append("Properties.PREPROCESS_PATH: 여러 원본 루트의 경로 매핑을 검토해야 함.")
            anchors.extend((value, case) for value in preprocess_roots)
            for (value,) in _optional_rows(db, "ImportInfo", ("workspacePath",)):
                if isinstance(value, str) and value:
                    anchors.append((value, case))
            logs = _optional_rows(db, "InspectionFailItem", ("idTranslationUnit", "logPath"))
    except (OSError, sqlite3.Error, RecoveryError, zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
        problem = _record_failure(budget, index, case, exc)
        if not matched:
            return None
        missing.append(problem)

    pa_ini = index.get(_join(case, "pa.ini"))
    if pa_ini is None or not pa_ini.size:
        missing.append("pa.ini: 필요한 입력 파일이 없거나 비어 있음.")
    selected = [row for row in tus if row[1] in matching_ids]
    compiler_ids = {row[2] for row in selected}
    if len(compiler_ids) != 1 or any(not isinstance(value, int) or value <= 0 for value in compiler_ids):
        missing.append("TranslationUnit: 대상 소스의 단일 컴파일러 연결을 확인하지 못함.")
        compiler_id = None
    else:
        compiler_id = next(iter(compiler_ids))
    scoped_tus = [row for row in tus if row[2] == compiler_id] if compiler_id is not None else selected
    if len({row[0] for row in scoped_tus}) != len(scoped_tus):
        missing.append("TranslationUnit: 중복 TU ID가 있음.")
    global_member = index.get(_join(case, "global.db"))
    if global_member is None:
        missing.append("global.db: 필요한 입력 파일이 없음.")
    else:
        try:
            with _database(index, global_member, budget) as db:
                _columns(db, "Compiler", {"idCompiler", "confFile"})
                compiler_rows = _rows(db, "SELECT confFile FROM Compiler WHERE idCompiler=?", (compiler_id,))
                if len(compiler_rows) != 1 or not isinstance(compiler_rows[0][0], str) or not compiler_rows[0][0]:
                    missing.append("Compiler.confFile: 대상 컴파일러의 설정 경로를 확인하지 못함.")
                else:
                    reference = compiler_rows[0][0]
                    name = _portable(reference).name.casefold()
                    case_path = PurePosixPath(case)
                    configs = [member for member in index.files.values() if PurePosixPath(member.name).is_relative_to(case_path) and PurePosixPath(member.name).name.casefold() == name and name.endswith(".conf")]
                    if len(configs) != 1 or not configs[0].size:
                        missing.append("Compiler.confFile: 이름이 일치하는 설정 파일이 없거나 모호함.")
                    else:
                        anchors.append((str(_portable(reference).parent), str(PurePosixPath(configs[0].name).parent)))
        except (OSError, sqlite3.Error, RecoveryError, zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
            missing.append(_record_failure(budget, index, case, exc))

    requested_present = []
    for tu_id, source_id, _, active, _, excluded in scoped_tus:
        source = sources.get(source_id)
        if not isinstance(tu_id, int) or tu_id <= 0 or not isinstance(source, str) or not source:
            missing.append("TranslationUnit: TU ID 또는 원본 소스 연결이 잘못됨.")
            continue
        if active != 1 or excluded not in (0, None):
            missing.append(f"TranslationUnit[{tu_id}]: 비활성 또는 제외 상태의 자료를 함께 검토해야 함.")
        copied = _mapped(index, case, source, anchors)
        original = _portable(source)
        preprocessed = index.get(_join(case, f"src/preprocessed/{original.stem}_{tu_id}.i{original.suffix}"))
        if copied is None and preprocessed is None:
            missing.append(f"TranslationUnit[{tu_id}]: 복사된 소스 또는 알려진 전처리 입력이 없음.")
        if source_id in matching_ids:
            requested_present.append(copied is not None)
    linked_logs = []
    target_tus = {row[0] for row in selected}
    for tu_id, reference in logs:
        if tu_id in target_tus:
            member = _mapped(index, case, reference, anchors)
            if member is not None and PurePosixPath(member.name).suffix.casefold() in (".elog", ".log"):
                linked_logs.append(member)
    if not linked_logs:
        missing.append(".elog/.log: 대상 TU와 DB 경로로 연결된 오류 로그를 확인하지 못함.")
    return {"missing": missing, "logs": linked_logs, "source_present": bool(requested_present) and all(requested_present)}


def _candidate(index: _Index, requested: str, budget: _Budget) -> tuple[dict[str, Any] | None, list[str]]:
    cases = index.case_roots if index.case_roots is not None else sorted({str(PurePosixPath(member.name).parent) for member in index.files.values() if PurePosixPath(member.name).name.casefold() == "project.db"})
    matching: dict[str, dict] = {}
    for case in cases:
        inspected = _inspect_case(index, case, requested, budget)
        if inspected is not None:
            matching[case] = inspected
    if not matching:
        return None, []
    missing = list(index.missing)
    if len(cases) > 1:
        missing.append("MultiCase: 같은 입력에 여러 case가 있어 전체 범위 검토가 필요함.")
    for case, facts in matching.items():
        missing.extend(f"{case}: {item}" for item in facts["missing"])
    members = {member.name: member for facts in matching.values() for member in facts["logs"]}
    evidence = sorted(str(member.value.absolute()) for member in members.values() if isinstance(member.value, Path))
    hint = re.fullmatch(r"issue_(NoSrc_)?([A-Za-z0-9][A-Za-z0-9_-]{0,255})\.zip", index.path.name, re.I) if index.kind == "zip" else None
    return {
        "path": str(index.path.absolute()), "kind": index.kind, "case_roots": cases,
        "matching_case_roots": list(matching), "source_path": requested,
        "complete": not missing, "missing": sorted(set(missing)), "modified_at": index.modified_at,
        "source_files_present": all(facts["source_present"] for facts in matching.values()),
        "archive_evidence": sorted(members) if index.kind == "zip" else [],
        "issue_id": hint.group(2) if hint else None,
        "source_included_hint": not bool(hint.group(1)) if hint else None,
    }, evidence


def search_artifacts(
    source_path: str,
    roots: list[Path],
    *,
    max_files: int = 20000,
    max_depth: int = 8,
    max_candidates: int = 100,
) -> dict[str, Any]:
    """지정한 로컬 범위에서 정확한 SourceFile.path를 가진 폴더·ZIP을 찾는다.

    roots는 검색 범위이며 폴더 또는 단일 ZIP 경로를 받는다. 폴더 후보는
    project.db의 부모, 형제 case가 있으면 공통 직계 부모를 유지한다.
    max_files는 파일뿐 아니라 폴더와 ZIP 목록 항목도 함께 제한한다.
    evidence_files는 DB의 대상 TU와 연결된 디스크 로그만 포함한다.
    ZIP 로그는 추출하지 않고 후보의 archive_evidence에 내부 경로만 기록한다.
    """
    if not isinstance(source_path, str) or not source_path.strip() or "\x00" in source_path or not _portable(source_path).is_absolute():
        raise RecoveryError("MissingSource", "정확한 원본 소스의 절대 경로가 필요함.")
    for name, value, minimum in (("max_files", max_files, 1), ("max_depth", max_depth, 0), ("max_candidates", max_candidates, 1)):
        if type(value) is not int or value < minimum:
            raise RecoveryError("InvalidSearchLimit", f"{name}은 {minimum} 이상의 정수여야 함.")
    if not isinstance(roots, (list, tuple)):
        raise RecoveryError("InvalidSearchRoots", "검색 범위는 로컬 경로 목록이어야 함.")
    requested = normalize_source_path(source_path)
    budget = _Budget(max_files)
    result: dict[str, Any] = {"candidates": [], "evidence_files": [], "searched_roots": [], "warnings": budget.warnings, "truncated": False}
    prepared: list[tuple[Path, bool]] = []
    seen_roots: set[Path] = set()
    if len(roots) > max_files:
        budget.warn("SearchRootLimit", "roots", "검색 루트 수가 한도를 넘어 일부만 확인함.", limit=True)
    for raw in roots[:max_files]:
        try:
            path = Path(raw).absolute()
            if path in seen_roots:
                continue
            seen_roots.add(path)
            _check_chain(path)
            info = path.lstat()
            is_directory = stat.S_ISDIR(info.st_mode)
            if not is_directory and (not stat.S_ISREG(info.st_mode) or path.suffix.casefold() != ".zip" or info.st_nlink > 1):
                raise RecoveryError("UnsupportedRoot", "검색 루트는 일반 폴더 또는 링크가 아닌 ZIP이어야 함.")
            ancestry = (path, *path.parents) if is_directory else path.parents
            if any(parent.name.casefold() in SKIP_DIRECTORIES or _run_folder(parent) for parent in ancestry):
                budget.warn("SkippedDirectory", path, "제외 폴더 또는 기존 복구 실행 폴더이므로 검색하지 않음.")
                continue
            prepared.append((path, is_directory))
        except (OSError, RecoveryError, TypeError, ValueError) as exc:
            code = exc.code if isinstance(exc, RecoveryError) else "UnreadableRoot"
            budget.warn(code, str(raw), "검색 루트가 없거나 안전하게 접근할 수 없음.")
    # 넓은 루트를 먼저 처리해 중첩 루트 때문에 같은 case를 잘라서 반환하지 않는다.
    minimal: list[tuple[Path, bool]] = []
    for path, is_directory in sorted(prepared, key=lambda item: (len(item[0].parts), str(item[0]).casefold())):
        if not any(directory and path.is_relative_to(parent) for parent, directory in minimal):
            minimal.append((path, is_directory))
    seen_artifacts: set[Path] = set()
    evidence: set[str] = set()
    stop = False
    for root, is_directory in minimal:
        if stop or not budget.take(1, root):
            break
        result["searched_roots"].append(str(root))
        if is_directory:
            files, problems, incomplete = _scan_directory(root, budget, max_depth)
            indexes = _directory_indexes(root, files, problems, incomplete)
            items = [(index.path, index) for index in indexes]
            items.extend((path, None) for path in files if path.suffix.casefold() == ".zip")
        else:
            items = [(root, None)]
        for path, index in sorted(items, key=lambda item: str(item[0]).casefold()):
            if path in seen_artifacts:
                continue
            if len(result["candidates"]) >= max_candidates:
                budget.warn("CandidateLimit", path, "후보 수 한도로 나머지 자료를 확인하지 못함.", limit=True)
                stop = True
                break
            seen_artifacts.add(path)
            try:
                if index is not None:
                    candidate, logs = _candidate(index, requested, budget)
                else:
                    _check_chain(path)
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                        raise RecoveryError("UnsafePath", "ZIP이 일반 파일이 아니거나 링크로 바뀜.")
                    count = _zip_header(path, info.st_size, budget)
                    with zipfile.ZipFile(path) as archive:
                        index = _zip_index(path, archive, max_depth, count)
                        candidate, logs = _candidate(index, requested, budget)
                if candidate is not None:
                    result["candidates"].append(candidate)
                    evidence.update(logs)
            except (OSError, RecoveryError, sqlite3.Error, zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
                code = exc.code if isinstance(exc, RecoveryError) else "UnreadableArtifact"
                message = exc.message if isinstance(exc, RecoveryError) else "자료를 안전하게 검사할 수 없음."
                budget.warn(code, path, message, limit=code.endswith("Limit"))
    result["candidates"].sort(key=lambda item: item["path"].casefold())
    result["evidence_files"] = sorted(evidence)
    result["truncated"] = budget.truncated
    return result
