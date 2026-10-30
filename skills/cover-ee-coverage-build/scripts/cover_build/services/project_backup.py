from __future__ import annotations

import hashlib
import html
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.core import errors


MANIFEST_SCHEMA = "cover-project-file-patch-backup/v1"
_CMAKE_KEYS = {
    "c_compiler": "CMAKE_C_COMPILER",
    "cxx_compiler": "CMAKE_CXX_COMPILER",
    "ar": "CMAKE_AR",
    "linker": "CMAKE_LINKER",
}
_BOMS = (
    (b"\xef\xbb\xbf", "UTF-8", "UTF-8"),
    (b"\xff\xfe", "UTF-16LE", "UTF-16LE"),
    (b"\xfe\xff", "UTF-16BE", "UTF-16BE"),
)


def _newline_style(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    values = sum(value > 0 for value in (crlf, lf, cr))
    if values > 1:
        return "MIXED"
    if crlf:
        return "CRLF"
    if lf:
        return "LF"
    if cr:
        return "CR"
    return "NONE"


def _decode(path: Path, data: bytes) -> tuple[str, str, str]:
    for bom, encoding, codec in _BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode(codec), encoding, bom.hex().upper()
    try:
        return data.decode("utf-8"), "UTF-8", "NONE"
    except UnicodeDecodeError as exc:
        raise errors.ExecutorError("PROJECT_FILE_PATCH_ENCODING_UNSUPPORTED", "Project patch target is not UTF-8/UTF-16 text", [str(path)]) from exc


def _encode(text: str, encoding: str, bom: str) -> bytes:
    codecs = {"UTF-8": "utf-8", "UTF-16LE": "utf-16le", "UTF-16BE": "utf-16be"}
    raw = text.encode(codecs[encoding])
    if bom == "EFBBBF":
        return b"\xef\xbb\xbf" + raw
    if bom == "FFFE":
        return b"\xff\xfe" + raw
    if bom == "FEFF":
        return b"\xfe\xff" + raw
    return raw


def _record(path: Path, project_root: Path) -> dict[str, Any]:
    data = path.read_bytes()
    _text, encoding, bom = _decode(path, data)
    stat = path.stat()
    return {
        "path": str(path),
        "relativePath": str(path.relative_to(project_root)).replace("\\", "/"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "modifiedNs": stat.st_mtime_ns,
        "encoding": encoding,
        "bom": bom,
        "newline": _newline_style(data),
    }


def _safe_backup_name(relative_path: str) -> str:
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest() + ".bin"


def _is_project_file(project_root: Path, value: Path) -> bool:
    return value.is_file() and pathutil.path_within(value, project_root)


def _read_if_text(path: Path) -> str | None:
    try:
        return _decode(path, path.read_bytes())[0]
    except errors.ExecutorError:
        return None


def _xml_attribute(tag: str, name: str) -> tuple[str, tuple[int, int]] | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1", tag, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    return html.unescape(match.group(2)), match.span(2)


def _stm32_tool_role(tag: str) -> str | None:
    descriptors = [
        value
        for name in ("id", "name", "superClass")
        if (attribute := _xml_attribute(tag, name)) is not None
        for value in [attribute[0].casefold()]
    ]
    descriptor = " ".join(descriptors)
    if "tool.cpp.compiler" in descriptor or "g++ compiler" in descriptor:
        return "cxx_compiler"
    if "tool.c.compiler" in descriptor or "gcc compiler" in descriptor:
        return "c_compiler"
    if "archiver" in descriptor:
        return "ar"
    if "linker" in descriptor:
        return "linker"
    return None


def _strip_outer_quotes(value: str) -> str:
    command = value.strip()
    if len(command) >= 2 and command[0] == command[-1] and command[0] in {"\"", "'"}:
        return command[1:-1].strip()
    return command


def _absolute_tool_command(value: str) -> bool:
    command = _strip_outer_quotes(value)
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", command)
        or command.startswith("\\\\")
        or command.startswith("/")
    )


def _tool_basename(value: str) -> str:
    return re.split(r"[\\/]", _strip_outer_quotes(value))[-1].casefold()


def _replacement_tool_command(original: str, wrapper: str) -> str:
    command = original.strip()
    quote = command[0] if len(command) >= 2 and command[0] == command[-1] and command[0] in {"\"", "'"} else ""
    if not quote and re.search(r"\s", wrapper):
        quote = '"'
    return f"{quote}{wrapper}{quote}" if quote else wrapper


def _stm32_configuration_blocks(text: str, configuration: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for block in re.finditer(r"<cconfiguration\b[^>]*>.*?</cconfiguration>", text, re.IGNORECASE | re.DOTALL):
        try:
            root = ET.fromstring(block.group(0))
        except ET.ParseError:
            continue
        names = {
            node.attrib["name"]
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] in {"cconfiguration", "configuration"}
            and node.attrib.get("name")
        }
        if configuration in names:
            matches.append(block)
    if len(matches) != 1:
        raise errors.ExecutorError(
            "STM32CUBEIDE_CONFIGURATION_SCOPE_UNRESOLVED",
            "The selected STM32CubeIDE configuration does not resolve to one .cproject block",
            [configuration, str(len(matches))],
        )
    return matches


def stm32cubeide_absolute_tool_commands(cproject: Path, configuration: str) -> list[dict[str, str]]:
    """Return literal absolute managed-tool commands for one selected configuration."""
    path = pathutil.canonical(cproject)
    text = _decode(path, path.read_bytes())[0]
    block = _stm32_configuration_blocks(text, configuration)[0].group(0)
    commands: list[dict[str, str]] = []
    for tool in re.finditer(r"<tool\b[^>]*>", block, re.IGNORECASE | re.DOTALL):
        command = _xml_attribute(tool.group(0), "command")
        role = _stm32_tool_role(tool.group(0))
        if command is not None and role is not None and _absolute_tool_command(command[0]):
            commands.append({"role": role, "command": command[0]})
    return commands


def cmake_patch_candidates(project_root: Path, entry: Path, references: list[str]) -> list[Path]:
    """Include the owning CMakeLists and only referenced .cmake compiler overrides."""
    root_entry = pathutil.canonical(entry)
    candidates = [root_entry]
    pattern = re.compile(r"\bCMAKE_(?:C|CXX)_COMPILER\b|\bCMAKE_(?:AR|LINKER)\b", re.IGNORECASE)
    for raw in references:
        path = pathutil.canonical(raw, False)
        if path == root_entry or path.suffix.casefold() != ".cmake" or not _is_project_file(project_root, path):
            continue
        text = _read_if_text(path)
        if text is not None and pattern.search(text):
            candidates.append(path)
    return sorted(set(candidates), key=lambda value: str(value).casefold())


def make_patch_candidates(
    project_root: Path,
    makefile: Path,
    references: list[str],
    build_scripts: list[str],
    real_tools: dict[str, str],
) -> list[Path]:
    """Select only project Make/includes/scripts that directly name an observed tool."""
    names = {Path(value).name.casefold() for value in real_tools.values()}
    names.update({"gcc", "g++", "cc", "c++", "ar", "ld"})
    pattern = re.compile(r"(?:^|[\s\t])(?:" + "|".join(re.escape(name) for name in sorted(names)) + r")(?:\s|$)", re.IGNORECASE | re.MULTILINE)
    candidates = [pathutil.canonical(makefile)]
    for raw in [*references, *build_scripts]:
        path = pathutil.canonical(raw, False)
        if not _is_project_file(project_root, path):
            continue
        if path.suffix.casefold() not in {".mk", ".make", ".mak", ".sh", ".bash", ".cmd", ".bat", ".ps1"} and path.name.casefold() not in {"makefile", "gnumakefile"}:
            continue
        text = _read_if_text(path)
        if text is not None and pattern.search(text):
            candidates.append(path)
    return sorted(set(candidates), key=lambda value: str(value).casefold())


@dataclass
class ProjectFilePatch:
    job: dict[str, Any]
    project_root: Path
    kind: str
    records: list[dict[str, Any]]
    manifest_path: Path
    backup_directory: Path

    @classmethod
    def create(cls, job: dict[str, Any], kind: str, paths: list[Path]) -> "ProjectFilePatch":
        project_root = pathutil.canonical(job["projectRoot"])
        unique = sorted({pathutil.canonical(path) for path in paths}, key=lambda value: str(value).casefold())
        if not unique or any(not _is_project_file(project_root, path) for path in unique):
            raise errors.ExecutorError("PROJECT_FILE_PATCH_SCOPE_INVALID", "Project patch targets must be owned files below projectRoot")
        temporary = Path(job["directories"]["temporary"])
        backup_directory = temporary / "project-file-patch-backups"
        manifest_path = temporary / "project-file-patch-manifest.json"
        records = [_record(path, project_root) for path in unique]
        patch = cls(job, project_root, kind, records, manifest_path, backup_directory)
        patch._write_manifest("BACKED_UP")
        return patch

    def _write_manifest(self, status: str) -> None:
        payload = fileio.add_integrity(
            {
                "schema": MANIFEST_SCHEMA,
                "jobId": self.job["jobId"],
                "kind": self.kind,
                "status": status,
                "backupDirectory": str(self.backup_directory),
                "files": self.records,
            }
        )
        fileio.atomic_write_json(self.manifest_path, payload)

    def backup(self) -> None:
        self.backup_directory.mkdir(parents=True, exist_ok=True)
        for record in self.records:
            path = Path(record["path"])
            current = path.read_bytes()
            if hashlib.sha256(current).hexdigest() != record["sha256"]:
                raise errors.ExecutorError("PROJECT_FILE_CONCURRENT_CHANGE", "Project file changed before backup", [str(path)])
            (self.backup_directory / _safe_backup_name(record["relativePath"])).write_bytes(current)
            record["backupPath"] = str(self.backup_directory / _safe_backup_name(record["relativePath"]))
        self._write_manifest("BACKED_UP")

    def apply_cmake(self, wrappers: dict[str, str]) -> list[str]:
        replacements: list[str] = []
        key_values = {_CMAKE_KEYS[role]: value for role, value in wrappers.items() if role in _CMAKE_KEYS}
        assignment = re.compile(r"(?im)^(\s*set\s*\(\s*(CMAKE_(?:(?:C|CXX)_COMPILER|AR|LINKER))\s+)(\"[^\"]*\"|[^\s\)]+)")
        try:
            for record in self.records:
                path = Path(record["path"])
                data = path.read_bytes()
                text, encoding, bom = _decode(path, data)
                text, count = assignment.subn(
                    lambda match: match.group(1) + f'"{key_values[match.group(2).upper()]}"' if match.group(2).upper() in key_values else match.group(0),
                    text,
                )
                if path.name.casefold() == "cmakelists.txt":
                    lines = ["# COVER temporary wrapper injection; restored after this job"]
                    lines.extend(f'set({key} "{value}" CACHE FILEPATH "" FORCE)' for key, value in key_values.items())
                    text = "\n".join(lines) + "\n" + text
                    count += len(lines)
                if count:
                    self._write_patched(record, _encode(text, encoding, bom))
                    replacements.append(str(path))
        except BaseException:
            self.restore()
            raise
        self._write_manifest("PATCHED")
        return replacements

    def apply_make(self, wrappers: dict[str, str], tool_variables: dict[str, str], real_tools: dict[str, str]) -> list[str]:
        names = {Path(value).name.casefold() for value in real_tools.values()}
        names.update({"gcc", "g++", "cc", "c++", "ar", "ld"})
        command_names = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
        variable_values = {tool_variables[role]: value for role, value in wrappers.items() if role in tool_variables}
        variable_names = "|".join(re.escape(name) for name in sorted(variable_values, key=len, reverse=True))
        assignment = re.compile(rf"(?im)^(\s*({variable_names})\s*(?:[:+?]?=)\s*)(\"[^\"]*\"|[^\s#]+)") if variable_names else None
        command = re.compile(rf"(?im)^(\s*\t\s*[@+\-]?\s*)({command_names})(?=\s|$)")
        role_names = {
            "c_compiler": {"gcc", "cc", Path(real_tools.get("c_compiler", "")).name.casefold()},
            "cxx_compiler": {"g++", "c++", Path(real_tools.get("cxx_compiler", "")).name.casefold()},
            "ar": {"ar", Path(real_tools.get("ar", "")).name.casefold()},
            "linker": {"ld", Path(real_tools.get("linker", "")).name.casefold()},
        }

        def replace_command(match: re.Match[str]) -> str:
            name = match.group(2).casefold()
            role = next((item for item, values in role_names.items() if name in values), None)
            return match.group(1) + wrappers[role] if role in wrappers else match.group(0)
        changed: list[str] = []
        try:
            for record in self.records:
                path = Path(record["path"])
                data = path.read_bytes()
                text, encoding, bom = _decode(path, data)
                count = 0
                if assignment:
                    text, assigned = assignment.subn(lambda match: match.group(1) + f'"{variable_values[match.group(2)]}"', text)
                    count += assigned
                text, invoked = command.subn(replace_command, text)
                count += invoked
                if count:
                    self._write_patched(record, _encode(text, encoding, bom))
                    changed.append(str(path))
        except BaseException:
            self.restore()
            raise
        if not changed:
            raise errors.ExecutorError("PROJECT_FILE_PATCH_TARGET_UNAVAILABLE", "No project-owned Make file directly selected a compiler to patch")
        self._write_manifest("PATCHED")
        return changed

    def apply_stm32cubeide(self, configuration: str, wrappers: dict[str, str]) -> list[dict[str, str]]:
        """Temporarily replace absolute managed-tool commands in one .cproject configuration."""
        if len(self.records) != 1 or Path(self.records[0]["path"]).name.casefold() != ".cproject":
            raise errors.ExecutorError(
                "STM32CUBEIDE_CPROJECT_PATCH_SCOPE_INVALID",
                "The STM32CubeIDE tool-command fallback must target exactly one project-owned .cproject",
            )
        try:
            record = self.records[0]
            path = Path(record["path"])
            data = path.read_bytes()
            text, encoding, bom = _decode(path, data)
            configuration_block = _stm32_configuration_blocks(text, configuration)[0]
            block = configuration_block.group(0)
            replacements: list[tuple[int, int, str]] = []
            changed: list[dict[str, str]] = []
            required: list[dict[str, str]] = []
            for tool in re.finditer(r"<tool\b[^>]*>", block, re.IGNORECASE | re.DOTALL):
                tag = tool.group(0)
                command = _xml_attribute(tag, "command")
                role = _stm32_tool_role(tag)
                if command is None or role not in wrappers or not _absolute_tool_command(command[0]):
                    continue
                wrapper = str(wrappers[role])
                required.append({"role": role, "command": command[0]})
                if _tool_basename(command[0]) != _tool_basename(wrapper):
                    continue
                value_start = configuration_block.start() + tool.start() + command[1][0]
                value_end = configuration_block.start() + tool.start() + command[1][1]
                replacement = _replacement_tool_command(command[0], wrapper)
                replacements.append((value_start, value_end, html.escape(replacement, quote=True)))
                changed.append({"role": role, "originalCommand": command[0], "wrapperCommand": wrapper})
            if not required or len(changed) != len(required):
                matched = {(item["role"], item["originalCommand"]) for item in changed}
                unmatched = [item for item in required if (item["role"], item["command"]) not in matched]
                raise errors.ExecutorError(
                    "STM32CUBEIDE_ABSOLUTE_TOOL_COMMAND_UNMATCHED",
                    "Every absolute STM32CubeIDE native tool command must match the observed wrapper basename",
                    [configuration, *[f'{item["role"]}={item["command"]}' for item in unmatched]],
                )
            for start, end, replacement in sorted(replacements, reverse=True):
                text = text[:start] + replacement + text[end:]
            self._write_patched(record, _encode(text, encoding, bom))
            record["stm32ToolCommandOverrides"] = changed
            self._write_manifest("PATCHED")
            return changed
        except BaseException:
            self.restore()
            raise

    def capture_external_patch(self) -> list[str]:
        """Record hashes after an approved product tool modified the backed-up files."""
        changed: list[str] = []
        for record in self.records:
            path = Path(record["path"])
            current = path.read_bytes()
            current_hash = hashlib.sha256(current).hexdigest()
            if current_hash != record["sha256"]:
                record["patchedSha256"] = current_hash
                changed.append(str(path))
        self._write_manifest("PATCHED" if changed else "UNCHANGED")
        return changed

    def promote_side_by_side(self) -> list[dict[str, Any]]:
        """Keep a verified patch and place its original beside the source file."""
        promoted: list[dict[str, Any]] = []
        for record in self.records:
            if "patchedSha256" not in record:
                continue
            path = Path(record["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != record["patchedSha256"]:
                raise errors.ExecutorError(
                    "PROJECT_FILE_CONCURRENT_CHANGE",
                    "Project file changed before its COVER patch could be retained",
                    [str(path)],
                )
            side_by_side = path.with_name(path.name + ".cover-ee-backup")
            original = Path(record["backupPath"]).read_bytes()
            if side_by_side.exists():
                if hashlib.sha256(side_by_side.read_bytes()).hexdigest() != record["sha256"]:
                    raise errors.ExecutorError(
                        "PROJECT_FILE_BACKUP_CONFLICT",
                        "An existing side-by-side COVER backup does not match the current original",
                        [str(side_by_side)],
                    )
            else:
                side_by_side.write_bytes(original)
            record["persistentBackupPath"] = str(side_by_side)
            promoted.append(
                {
                    "path": str(path),
                    "sha256": record["sha256"],
                    "patchedSha256": record["patchedSha256"],
                    "backupPath": str(side_by_side),
                }
            )
        self._write_manifest("PERSISTED")
        return promoted

    def _write_patched(self, record: dict[str, Any], data: bytes) -> None:
        path = Path(record["path"])
        before = path.read_bytes()
        if hashlib.sha256(before).hexdigest() != record["sha256"]:
            raise errors.ExecutorError("PROJECT_FILE_CONCURRENT_CHANGE", "Project file changed before patch", [str(path)])
        temporary = path.with_name(path.name + ".cover-tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        record["patchedSha256"] = hashlib.sha256(data).hexdigest()

    def restore(self) -> dict[str, Any]:
        restored: list[str] = []
        for record in self.records:
            if "patchedSha256" not in record:
                continue
            path = Path(record["path"])
            current = path.read_bytes()
            patched_hash = record["patchedSha256"]
            if hashlib.sha256(current).hexdigest() != patched_hash:
                self._write_manifest("RESTORE_BLOCKED_CONCURRENT_CHANGE")
                raise errors.ExecutorError("PROJECT_FILE_CONCURRENT_CHANGE", "Project file changed while COVER patch was active; refusing to overwrite", [str(path)])
            backup = Path(record["backupPath"])
            restored_data = backup.read_bytes()
            temporary = path.with_name(path.name + ".cover-restore-tmp")
            temporary.write_bytes(restored_data)
            os.replace(temporary, path)
            os.utime(path, ns=(record["modifiedNs"], record["modifiedNs"]))
            if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                raise errors.ExecutorError("PROJECT_FILE_RESTORE_FAILED", "Restored project file does not match the backup hash", [str(path)])
            restored.append(str(path))
        self._write_manifest("RESTORED")
        return {"manifest": str(self.manifest_path), "restoredFiles": restored, "verified": True}
