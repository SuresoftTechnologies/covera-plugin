"""Resolve the native C++ compiler used only for COVER glue-code compilation."""

from __future__ import annotations

import locale
import os
import re
from pathlib import Path
from typing import Any


from cover_build.core.errors import WorkflowWaiting
from cover_build.common import pathutil
from cover_build.common import toolchains
from cover_build.core import errors


_COMPANION_NAMES = (
    (re.compile(r"^(?P<prefix>.*)gcc(?P<version>-(?:\d+(?:\.\d+)*))?(?P<exe>\.exe)?$", re.IGNORECASE), r"\g<prefix>g++\g<version>\g<exe>"),
    (re.compile(r"^(?P<prefix>.*)clang(?P<version>-(?:\d+(?:\.\d+)*))?(?P<exe>\.exe)?$", re.IGNORECASE), r"\g<prefix>clang++\g<version>\g<exe>"),
)

# These compiler drivers select C or C++ from the input-file extension.  When a
# C-only original build observes one of them, the same executable is therefore
# the target-compatible compiler for COVER's generated .cpp glue source.
_DUAL_LANGUAGE_COMPILER_NAMES = {
    "cl",
    "cl.exe",
}

_STM32_INCOMPATIBLE_LINK_FLAGS = {
    "-lwsock32",
    "-lshlwapi",
}


def _resolve_observed_path(value: str, search_directories: list[str | Path]) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
        raise WorkflowWaiting(
            "COVER_GLUE_COMPILER_UNRESOLVED",
            "The compiler used for COVER glue code does not exist",
            {"glueCodeCompiler": []},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[{"observedTool": value, "resolvedPath": str(resolved)}],
        )
    names = [candidate.name]
    if os.name == "nt" and not candidate.name.casefold().endswith(".exe"):
        names.append(f"{candidate.name}.exe")
    matches: list[Path] = []
    for directory_value in search_directories:
        directory = Path(directory_value).expanduser().resolve()
        for name in names:
            path = directory / name
            if path.is_file() and not any(pathutil.same_path(path, existing) for existing in matches):
                matches.append(path.resolve())
    if len(matches) != 1:
        raise WorkflowWaiting(
            "COVER_GLUE_COMPILER_UNRESOLVED",
            "The compiler used for COVER glue code could not be resolved to one executable",
            {"glueCodeCompiler": [str(path) for path in matches]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[{"observedTool": value, "searchDirectories": [str(path) for path in search_directories]}],
        )
    return matches[0]


def resolve(
    toolchain: dict[str, Any],
    *,
    search_directories: list[str | Path] | None = None,
    agent_root: str | Path | None = None,
    selected_compiler: str | None = None,
) -> dict[str, str]:
    """Select a real target C++ compiler without adding an Agent tool mapping."""
    directories = list(search_directories or [])
    tools = dict(toolchain.get("invokedTools") or toolchain.get("tools") or {})
    if tools.get("cxx_compiler"):
        compiler = _resolve_observed_path(str(tools["cxx_compiler"]), directories)
        provenance = "ORIGINAL_BUILD_OBSERVED"
    else:
        c_compiler_value = tools.get("c_compiler")
        if not c_compiler_value:
            raise WorkflowWaiting(
                "COVER_GLUE_COMPILER_UNRESOLVED",
                "COVER glue-code compilation requires a target C++ compiler",
                {"glueCodeCompiler": []},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
            )
        c_compiler = _resolve_observed_path(str(c_compiler_value), directories)
        if c_compiler.name.casefold() in _DUAL_LANGUAGE_COMPILER_NAMES:
            compiler = c_compiler
            provenance = "OBSERVED_DUAL_LANGUAGE_COMPILER"
        else:
            companion_name = None
            for pattern, replacement in _COMPANION_NAMES:
                if pattern.fullmatch(c_compiler.name):
                    companion_name = pattern.sub(replacement, c_compiler.name)
                    break
            companion = c_compiler.with_name(companion_name) if companion_name else None
            if companion is not None and companion.is_file():
                compiler = companion.resolve()
                provenance = "SIBLING_OF_OBSERVED_C_COMPILER"
            elif selected_compiler:
                selected = _resolve_observed_path(selected_compiler, directories)
                if selected.parent != c_compiler.parent:
                    raise errors.ExecutorError(
                        "GLUE_COMPILER_TOOLCHAIN_MISMATCH",
                        "The selected C++ driver must be in the observed C compiler directory",
                        [{"cCompiler": str(c_compiler), "selectedGlueCompiler": str(selected)}],
                    )
                compiler = selected
                provenance = "DECISION_VERIFIED_SAME_DIRECTORY_COMPILER"
            else:
                raise WorkflowWaiting(
                    "COVER_GLUE_COMPILER_UNRESOLVED",
                    "The observed C compiler has no proven same-toolchain C++ driver for COVER glue code",
                    {"glueCodeCompiler": []},
                    decision_owner="MODEL",
                    purpose="BUILD_ENVIRONMENT",
                    evidence=[{"cCompiler": str(c_compiler), "expectedCompanion": str(companion or "")}],
                )

    if agent_root is not None:
        root = Path(agent_root).expanduser().resolve()
        if pathutil.path_within(compiler, root):
            raise errors.ExecutorError(
                "WRAPPER_SELF_REFERENCE",
                "COVER glue-code compiler must be a real target compiler outside the Agent installation",
                [str(compiler)],
            )
    return {
        "path": str(compiler),
        "provenance": provenance,
        "mappingPolicy": "GLUE_ONLY_NOT_AGENT_WRAPPER_MAPPING",
    }


def _source_config(agent_root: Path, requested: str) -> Path:
    candidate = Path(requested).expanduser()
    if candidate.is_absolute():
        return pathutil.canonical(candidate)
    if candidate.name != requested or any(separator in requested for separator in ("/", "\\")):
        raise errors.ExecutorError(
            "INVALID_COVER_CONFIG",
            "COVER config must be an absolute path or a file name under <agent-root>/conf",
        )
    return pathutil.canonical(agent_root / "conf" / requested)


def _patch_glue_compiler(
    source: Path,
    destination: Path,
    compiler: Path,
    *,
    remove_stm32_incompatible_link_flags: bool = False,
) -> list[str]:
    original = source.read_bytes()
    encoding = "mbcs" if os.name == "nt" else (locale.getpreferredencoding(False) or "utf-8")
    try:
        compiler_bytes = str(compiler).encode(encoding)
    except UnicodeEncodeError:
        compiler_bytes = str(compiler).encode("utf-8")
    if b"\r" in compiler_bytes or b"\n" in compiler_bytes:
        raise errors.ExecutorError("INVALID_GLUE_COMPILER", "Glue-code compiler path contains a line break")
    pattern = re.compile(rb"(?m)^(?P<prefix>[ \t]*GlueCodeCompiler[ \t]*=[ \t]*).*$")
    matches = list(pattern.finditer(original))
    if len(matches) > 1:
        raise errors.ExecutorError(
            "INVALID_COVER_CONFIG",
            "COVER config contains more than one GlueCodeCompiler setting",
            [str(source)],
        )
    if matches:
        patched = pattern.sub(lambda match: match.group("prefix") + compiler_bytes, original, count=1)
    else:
        newline = b"\r\n" if b"\r\n" in original else b"\n"
        patched = original + (b"" if original.endswith((b"\r", b"\n")) else newline) + b"GlueCodeCompiler = " + compiler_bytes + newline

    removed_flags: list[str] = []
    if remove_stm32_incompatible_link_flags:
        link_pattern = re.compile(rb"(?m)^(?P<prefix>[ \t]*LinkAdditionalFlags[ \t]*=[ \t]*)(?P<value>.*)$")
        link_matches = list(link_pattern.finditer(patched))
        if len(link_matches) > 1:
            raise errors.ExecutorError(
                "INVALID_COVER_CONFIG",
                "COVER config contains more than one LinkAdditionalFlags setting",
                [str(source)],
            )
        if link_matches:
            raw_value = link_matches[0].group("value").decode(encoding, errors="replace")
            retained: list[str] = []
            for item in raw_value.split(";"):
                flag = item.strip()
                if flag.casefold() in _STM32_INCOMPATIBLE_LINK_FLAGS:
                    removed_flags.append(flag)
                elif flag:
                    retained.append(flag)
            retained_bytes = ";".join(retained).encode(encoding)
            patched = link_pattern.sub(
                lambda match: match.group("prefix") + retained_bytes,
                patched,
                count=1,
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(patched)
    return removed_flags


def prepare_runtime(
    job: dict[str, Any],
    toolchain: dict[str, Any],
    agent_root: str | Path,
    requested_cover_config: str,
    *,
    search_directories: list[str | Path] | None = None,
    platform_name: str | None = None,
    selected_compiler: str | None = None,
    remove_stm32_incompatible_link_flags: bool = False,
) -> dict[str, Any]:
    """Create the runtime environment/config needed by the native wrapper."""
    root = Path(agent_root).expanduser().resolve()
    selected = resolve(
        toolchain,
        search_directories=search_directories,
        agent_root=root,
        selected_compiler=selected_compiler,
    )
    compiler = Path(selected["path"])
    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        return {
            **selected,
            "coverConfig": None,
            "environment": {"COVER_CC": str(compiler)},
            "configurationMethod": "COVER_CC",
        }

    source = _source_config(root, requested_cover_config)
    toolchains.parse_cover_wrapper_config(source)
    destination = Path(job["directories"]["temporary"]) / "cover-glue-compiler.config"
    removed_link_flags = _patch_glue_compiler(
        source,
        destination,
        compiler,
        remove_stm32_incompatible_link_flags=remove_stm32_incompatible_link_flags,
    )
    values = toolchains.parse_cover_wrapper_config(destination)
    if values.get("GlueCodeCompiler") != str(compiler):
        raise errors.ExecutorError(
            "GLUE_COMPILER_CONFIG_MISMATCH",
            "Job-local COVER config does not contain the selected glue-code compiler",
            [str(destination)],
        )
    return {
        **selected,
        "coverConfig": str(destination.resolve()),
        "sourceCoverConfig": str(source),
        "sourceCoverConfigSha256": pathutil.sha256_file(source),
        "coverConfigSha256": pathutil.sha256_file(destination),
        "environment": {},
        "configurationMethod": "JOB_LOCAL_GLUE_CODE_COMPILER_CONFIG",
        "removedLinkAdditionalFlags": removed_link_flags,
    }
