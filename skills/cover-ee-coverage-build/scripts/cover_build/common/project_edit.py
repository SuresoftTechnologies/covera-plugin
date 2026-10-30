"""Customer project file edits that preserve encoding and keep side-by-side backups."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from cover_build.core import errors


def _replace_paths(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        updated = value
        for source, destination in replacements.items():
            updated = updated.replace(source, destination)
            updated = updated.replace(source.replace("\\", "/"), destination.replace("\\", "/"))
        return updated
    if isinstance(value, list):
        return [_replace_paths(child, replacements) for child in value]
    if isinstance(value, dict):
        return {key: _replace_paths(child, replacements) for key, child in value.items()}
    return value


def _contains_path_reference(value: Any, path: str | Path) -> bool:
    needle = str(Path(path).resolve()).replace("/", "\\").casefold()
    if isinstance(value, str):
        return needle in value.replace("/", "\\").casefold()
    if isinstance(value, list):
        return any(_contains_path_reference(child, path) for child in value)
    if isinstance(value, dict):
        return any(_contains_path_reference(child, path) for child in value.values())
    return False


def _decode_project_file(path: Path) -> tuple[str, str, bytes]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8", b"\xef\xbb\xbf"
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16le"), "utf-16le", b"\xff\xfe"
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16be"), "utf-16be", b"\xfe\xff"
    return raw.decode("utf-8"), "utf-8", b""


def _encode_project_file(text: str, encoding: str, bom: bytes) -> bytes:
    return bom + text.encode(encoding)


def _apply_file_change(
    setup_root: Path,
    path: Path,
    updated: bytes,
    kind: str,
    *,
    allow_create: bool = False,
) -> dict[str, Any] | None:
    path = path.resolve(strict=not allow_create)
    if path.exists() and not path.is_file():
        raise errors.ExecutorError(
            "PERSISTENT_SETUP_PATH_INVALID",
            "A persistent setup target is not a regular file",
            [str(path)],
        )
    original_existed = path.is_file()
    original = path.read_bytes() if original_existed else b""
    if original == updated:
        return None
    identity = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    backup = setup_root / "backup" / f"{identity}-{path.name}.original"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(original)
    temporary = path.with_name(path.name + ".cover-ee-tmp")
    temporary.write_bytes(updated)
    os.replace(temporary, path)
    return {
        "kind": kind,
        "path": str(path),
        "originalBackup": str(backup),
        "originalExisted": original_existed,
        "originalSha256": hashlib.sha256(original).hexdigest(),
        "appliedSha256": hashlib.sha256(updated).hexdigest(),
    }


def _append_before_closing(text: str, closing: str, fragment: str) -> str:
    index = text.lower().rfind(closing.lower())
    if index < 0:
        raise errors.ExecutorError(
            "PERSISTENT_PROJECT_FORMAT_UNSUPPORTED",
            f"Cannot find {closing} while applying the persistent COVER setup",
        )
    return text[:index] + fragment + text[index:]


def _restore_project_changes(changes: Iterable[dict[str, Any]]) -> None:
    for change in reversed(list(changes)):
        target = Path(change["path"])
        if not target.is_file():
            continue
        if hashlib.sha256(target.read_bytes()).hexdigest().casefold() != str(change["appliedSha256"]).casefold():
            continue
        if change.get("originalExisted", True):
            target.write_bytes(Path(change["originalBackup"]).read_bytes())
        else:
            target.unlink()


def _remove_new_side_backups(changes: Iterable[dict[str, Any]]) -> None:
    for change in changes:
        if change.get("sideBySideBackupCreated"):
            Path(change["sideBySideBackup"]).unlink(missing_ok=True)
