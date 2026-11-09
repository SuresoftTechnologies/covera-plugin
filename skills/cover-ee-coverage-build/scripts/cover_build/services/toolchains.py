"""Real tool verification and COVER wrapper configuration parsing."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import pathutil
from cover_build.core import errors


def parse_tool_argument(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Expected TOOL=REAL_PATH")
    name, path = value.split("=", 1)
    name = name.strip()
    if not name or Path(name).name != name:
        raise argparse.ArgumentTypeError("TOOL must be a file name such as cl.exe or gcc")
    return name, pathutil.canonical(path.strip())


def verify_toolchain(args: argparse.Namespace) -> dict[str, Any]:
    root = pathutil.canonical(args.agent_root)
    results: list[dict[str, Any]] = []
    for raw in args.tool:
        name, expected = parse_tool_argument(raw)
        wrapper = pathutil.canonical(root / "wrapper" / name)
        mapping_file = pathutil.canonical(root / "toolchaininfo" / name)
        mapped_text = mapping_file.read_text(encoding="utf-8-sig", errors="strict").strip()
        mapped = pathutil.canonical(mapped_text)
        if not pathutil.same_path(mapped, expected):
            raise errors.ExecutorError("MATCHING_TOOLCHAIN_NOT_FOUND", f"{name} maps to {mapped}, expected {expected}")
        if pathutil.same_path(wrapper, mapped):
            raise errors.ExecutorError("WRAPPER_RECURSION", f"{name} wrapper maps to itself")
        results.append(
            {
                "tool": name,
                "wrapper": str(wrapper),
                "mappingFile": str(mapping_file),
                "realTool": str(mapped),
                "wrapperSha256": pathutil.sha256_file(wrapper),
                "ready": True,
            }
        )
    return envelopes.envelope("VERIFYING_TOOLCHAIN", "SUCCEEDED", {"agentRoot": str(root), "tools": results})


def parse_cover_wrapper_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    if values.get("OnCoverage", "").casefold() != "true":
        raise errors.ExecutorError("COVER_DISABLED_IN_CONFIG", f"OnCoverage=true is required in {path}")
    required = ("PreprocessFlag", "PreprocessOutputFlag", "TmpPath")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise errors.ExecutorError("INVALID_COVER_CONFIG", f"Required GCC wrapper config values are missing in {path}", missing)
    return values
