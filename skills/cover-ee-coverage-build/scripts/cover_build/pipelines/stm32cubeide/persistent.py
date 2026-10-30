"""Reusable STM32CubeIDE setup: CDT project environment and Agent gcc.config application."""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable

from cover_build.common import pathutil
from cover_build.common import project_edit
from cover_build.core import errors


def _stm32_configuration_block(text: str, configuration: str) -> re.Match[str]:
    matches: list[re.Match[str]] = []
    for block in re.finditer(
        r"<cconfiguration\b[^>]*>.*?</cconfiguration>",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            element = ET.fromstring(block.group(0))
        except ET.ParseError:
            continue
        names = {
            node.attrib["name"]
            for node in element.iter()
            if node.tag.rsplit("}", 1)[-1] in {"cconfiguration", "configuration"}
            and node.attrib.get("name")
        }
        if configuration in names:
            matches.append(block)
    if len(matches) != 1:
        raise errors.ExecutorError(
            "PERSISTENT_STM32_CONFIGURATION_UNRESOLVED",
            "The selected STM32CubeIDE configuration does not resolve to one .cproject block",
            [configuration, str(len(matches))],
        )
    return matches[0]


def _prepend_windows_path(wrapper_directory: str, existing: str) -> str:
    wrapper = wrapper_directory.rstrip("\\/")
    values = [value.strip() for value in existing.split(";") if value.strip()]
    wrapper_key = wrapper.replace("/", "\\").casefold()
    retained = [
        value
        for value in values
        if value.rstrip("\\/").replace("/", "\\").casefold() != wrapper_key
    ]
    return ";".join([wrapper, *retained])


def _java_property_unescape(value: str) -> str:
    output: list[str] = []
    index = 0
    escapes = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
    while index < len(value):
        if value[index] != "\\" or index + 1 >= len(value):
            output.append(value[index])
            index += 1
            continue
        index += 1
        marker = value[index]
        if marker == "u" and index + 4 < len(value):
            candidate = value[index + 1:index + 5]
            if re.fullmatch(r"[0-9A-Fa-f]{4}", candidate):
                output.append(chr(int(candidate, 16)))
                index += 5
                continue
        output.append(escapes.get(marker, marker))
        index += 1
    return "".join(output)


def _java_property_escape(value: str) -> str:
    replacements = {
        "\\": "\\\\",
        "\t": "\\t",
        "\n": "\\n",
        "\r": "\\r",
        "\f": "\\f",
        ":": "\\:",
        "=": "\\=",
        "#": "\\#",
        "!": "\\!",
    }
    return "".join(replacements.get(character, character) for character in value)


def _stm32_configuration_id(cproject: Path, configuration: str) -> str:
    text = project_edit._decode_project_file(cproject)[0]
    block = _stm32_configuration_block(text, configuration)
    element = ET.fromstring(block.group(0))
    identifier = str(element.attrib.get("id") or "")
    if not identifier:
        raise errors.ExecutorError(
            "PERSISTENT_STM32_CONFIGURATION_ID_MISSING",
            "The selected STM32CubeIDE configuration has no persistent CDT identifier",
            [str(cproject), configuration],
        )
    return identifier


def _updated_stm32_core_preferences(
    preferences: Path,
    configuration_id: str,
    configuration: str,
    wrapper_directory: Path,
    cover_config_name: str,
    base_path: str,
) -> bytes:
    if preferences.is_file():
        raw = preferences.read_bytes()
        text, encoding, bom = project_edit._decode_project_file(preferences)
    else:
        raw = b""
        text = "eclipse.preferences.version=1\n"
        encoding = "utf-8"
        bom = b""
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    prefix = f"environment/project/{configuration_id}/"

    properties: dict[str, tuple[int, str]] = {}
    for index, line in enumerate(lines):
        if not line.startswith(prefix) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key[len(prefix):]] = (index, value)

    path_operation = _java_property_unescape(properties.get("PATH/operation", (-1, "prepend"))[1]).casefold()
    if path_operation == "remove":
        raise errors.ExecutorError(
            "PERSISTENT_STM32_PATH_CONFLICT",
            "The selected STM32CubeIDE configuration explicitly removes PATH",
            [str(preferences), configuration],
        )
    if path_operation not in {"prepend", "replace"}:
        raise errors.ExecutorError(
            "PERSISTENT_STM32_PATH_OPERATION_UNSUPPORTED",
            "The selected STM32CubeIDE PATH operation cannot preserve its ordering while placing the wrapper first",
            [str(preferences), configuration, path_operation],
        )
    for flag in ("append", "appendContributed"):
        existing_flag = properties.get(flag)
        if existing_flag is not None and _java_property_unescape(existing_flag[1]).casefold() != "true":
            raise errors.ExecutorError(
                "PERSISTENT_STM32_ENVIRONMENT_COMPOSITION_CONFLICT",
                "The selected STM32CubeIDE configuration does not inherit its normal build environment",
                [str(preferences), configuration, flag],
            )
    existing_path = _java_property_unescape(properties.get("PATH/value", (-1, ""))[1])
    if "PATH/value" not in properties:
        effective_path = base_path
    elif path_operation == "prepend":
        effective_path = ";".join(value for value in (existing_path, base_path) if value)
    else:
        effective_path = existing_path
    desired = {
        "PATH/delimiter": ";",
        "PATH/operation": "replace",
        "PATH/value": _prepend_windows_path(str(wrapper_directory), effective_path),
        "COVER_BR_CONFIG/delimiter": ";",
        "COVER_BR_CONFIG/operation": "replace",
        "COVER_BR_CONFIG/value": cover_config_name,
        "append": "true",
        "appendContributed": "true",
    }
    for suffix, value in desired.items():
        rendered = prefix + suffix + "=" + _java_property_escape(value)
        existing = properties.get(suffix)
        if existing is None:
            lines.append(rendered)
        else:
            lines[existing[0]] = rendered
    updated_text = newline.join(lines) + (newline if trailing_newline else "")
    updated = project_edit._encode_project_file(updated_text, encoding, bom)
    if updated == raw:
        return raw
    return updated


def _apply_stm32_ide_environment(
    setup_root: Path,
    details: dict[str, Any],
) -> list[dict[str, Any]]:
    cproject_value = details.get("cprojectFile")
    configuration = details.get("configuration")
    wrapper_value = details.get("wrapperDirectory")
    cover_config_name = details.get("coverConfigName")
    base_path = details.get("ideBasePath")
    if not all(isinstance(value, str) and value for value in (cproject_value, configuration, wrapper_value, cover_config_name, base_path)):
        raise errors.ExecutorError(
            "PERSISTENT_STM32_IDE_ENVIRONMENT_MISSING",
            "STM32CubeIDE persistence requires .cproject, configuration, wrapper directory, COVER config name, and verified base PATH",
        )
    cproject = Path(cproject_value).resolve(strict=True)
    project_root = Path(details.get("projectRoot") or setup_root.parent).resolve(strict=True)
    wrapper_directory = Path(wrapper_value).resolve(strict=True)
    preferences = cproject.parent / ".settings" / "org.eclipse.cdt.core.prefs"
    if (
        cproject.name.casefold() != ".cproject"
        or not pathutil.path_within(cproject, project_root)
        or not preferences.parent.is_dir()
        or (preferences.exists() and not preferences.is_file())
        or not pathutil.path_within(preferences, project_root)
        or not wrapper_directory.is_dir()
        or Path(cover_config_name).name != cover_config_name
    ):
        raise errors.ExecutorError(
            "PERSISTENT_STM32_IDE_ENVIRONMENT_INVALID",
            "STM32CubeIDE persistent build environment paths are invalid",
            [str(cproject), str(preferences), str(wrapper_directory), str(cover_config_name)],
        )
    configuration_id = _stm32_configuration_id(cproject, configuration)
    updated = _updated_stm32_core_preferences(
        preferences,
        configuration_id,
        configuration,
        wrapper_directory,
        cover_config_name,
        base_path,
    )
    change = project_edit._apply_file_change(
        setup_root,
        preferences,
        updated,
        "STM32CUBEIDE_BUILD_ENVIRONMENT",
        allow_create=True,
    )
    return [change] if change else []


def _apply_stm32_agent_config(
    setup_root: Path,
    config_assets: list[dict[str, Any]],
    details: dict[str, Any],
) -> list[dict[str, Any]]:
    staged_value = details.get("stagedAgentConfig")
    target_value = details.get("agentDefaultConfig")
    expected_hash = str(details.get("verifiedAgentConfigSha256") or "")
    if not isinstance(staged_value, str) or not isinstance(target_value, str) or not expected_hash:
        raise errors.ExecutorError(
            "PERSISTENT_STM32_AGENT_CONFIG_MISSING",
            "STM32CubeIDE persistence requires the verified staged config, its hash, and the Agent gcc.config target",
        )

    staged = Path(staged_value).resolve()
    target = Path(target_value).resolve(strict=True)
    promoted = next(
        (
            Path(item["path"])
            for item in config_assets
            if Path(item["source"]).resolve() == staged
        ),
        None,
    )
    if promoted is None or not promoted.is_file():
        raise errors.ExecutorError(
            "PERSISTENT_STM32_AGENT_CONFIG_MISSING",
            "The verified STM32 Agent config was not promoted into the project-local COVER setup",
            [str(staged)],
        )
    applied = promoted.read_bytes()
    applied_hash = hashlib.sha256(applied).hexdigest()
    if applied_hash.casefold() != expected_hash.casefold():
        raise errors.ExecutorError(
            "PERSISTENT_STM32_AGENT_CONFIG_MISMATCH",
            "The promoted STM32 Agent config no longer matches the configuration used by the verified build",
            [str(promoted)],
        )

    current = target.read_bytes()
    side_backup = target.with_name(target.name + ".cover-ee-backup")
    side_backup_created = False
    if side_backup.exists():
        if not side_backup.is_file():
            raise errors.ExecutorError(
                "PERSISTENT_STM32_AGENT_CONFIG_BACKUP_CONFLICT",
                "The STM32 Agent config backup path is not a regular file",
                [str(side_backup)],
            )
        original = side_backup.read_bytes()
        if current not in {original, applied}:
            raise errors.ExecutorError(
                "PERSISTENT_SETUP_CONCURRENT_CHANGE",
                "Agent gcc.config changed after its COVER backup was created; refusing to overwrite it",
                [str(target), str(side_backup)],
            )
    else:
        original = current
        temporary_backup = side_backup.with_name(side_backup.name + ".cover-ee-tmp")
        temporary_backup.write_bytes(original)
        os.replace(temporary_backup, side_backup)
        side_backup_created = True

    try:
        identity = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16]
        persistent_backup = setup_root / "backup" / f"{identity}-{target.name}.original"
        persistent_backup.parent.mkdir(parents=True, exist_ok=True)
        persistent_backup.write_bytes(original)
        if current != applied:
            temporary = target.with_name(target.name + ".cover-ee-tmp")
            temporary.write_bytes(applied)
            os.replace(temporary, target)
        return [
            {
                "kind": "STM32_AGENT_GCC_CONFIG",
                "path": str(target),
                "originalBackup": str(persistent_backup),
                "sideBySideBackup": str(side_backup),
                "sideBySideBackupCreated": side_backup_created,
                "originalSha256": hashlib.sha256(original).hexdigest(),
                "appliedSha256": applied_hash,
            }
        ]
    except BaseException:
        if target.is_file() and target.read_bytes() == applied and current != applied:
            temporary = target.with_name(target.name + ".cover-ee-tmp")
            temporary.write_bytes(current)
            os.replace(temporary, target)
        if side_backup_created:
            side_backup.unlink(missing_ok=True)
        raise


def _stm32_staged_config(details: dict[str, Any]) -> Path | None:
    staged_value = details.get("stagedAgentConfig")
    if not isinstance(staged_value, str):
        return None
    target_value = details.get("agentDefaultConfig")
    if not isinstance(target_value, str):
        raise errors.ExecutorError(
            "PERSISTENT_STM32_AGENT_CONFIG_MISSING",
            "STM32 staged-config cleanup requires the Agent gcc.config target",
        )
    staged = Path(staged_value).resolve()
    target = Path(target_value).resolve()
    if (
        staged.parent != target.parent
        or target.name.casefold() != "gcc.config"
        or target.parent.name.casefold() != "conf"
        or not staged.name.startswith("cover-glue-compiler-")
        or staged.suffix.casefold() != ".config"
    ):
        raise errors.ExecutorError(
            "PERSISTENT_STM32_AGENT_CONFIG_CLEANUP_UNSAFE",
            "Refusing to delete an STM32 staged config outside the Agent conf naming contract",
            [str(staged), str(target)],
        )
    return staged
