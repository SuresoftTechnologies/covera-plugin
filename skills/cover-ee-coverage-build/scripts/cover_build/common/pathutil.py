"""Path canonicalization, comparison, and hashing helpers."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.core import errors


def canonical(path: str | Path, must_exist: bool = True) -> Path:
    value = Path(path).expanduser()
    if must_exist:
        return value.resolve(strict=True)
    return value.resolve(strict=False)


def absolute_literal(path: str | Path, must_exist: bool = True) -> Path:
    """Return an absolute path without resolving an intentional path alias."""
    value = Path(os.path.abspath(os.path.expanduser(str(path))))
    if must_exist and not value.exists():
        raise FileNotFoundError(value)
    return value


def same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(canonical(left, False)))) == os.path.normcase(
        os.path.normpath(str(canonical(right, False)))
    )


def _executable_name_key(value: str | Path, platform: str | None = None) -> str:
    """Normalize only the optional Windows executable suffix for name matching."""
    name = Path(value).name.casefold()
    if (platform or os.name) == "nt" and name.endswith(".exe"):
        return name[:-4]
    return name


def compare_output_paths(original: list[str | Path], coverage: list[str | Path]) -> dict[str, Any]:
    """Compare coverage outputs with the paths observed during the original build.

    COVER identifies a native/managed Module by the absolute build output path.  A
    coverage build may change contents and timestamps, but it must not relocate the
    output.  Keep both ordered lists in the evidence so a stage can explain a
    missing, added, or renamed output instead of silently rebasing it.
    """
    original_paths = [str(canonical(path, False)) for path in original]
    coverage_paths = [str(canonical(path, False)) for path in coverage]
    original_keys = {os.path.normcase(os.path.normpath(path)) for path in original_paths}
    coverage_keys = {os.path.normcase(os.path.normpath(path)) for path in coverage_paths}
    missing = [path for path in original_paths if os.path.normcase(os.path.normpath(path)) not in coverage_keys]
    added = [path for path in coverage_paths if os.path.normcase(os.path.normpath(path)) not in original_keys]
    return {
        "originalOutputPaths": original_paths,
        "coverageOutputPaths": coverage_paths,
        "sameAsOriginal": not missing and not added and len(original_paths) == len(coverage_paths),
        "missingOriginalPaths": missing,
        "unexpectedCoveragePaths": added,
    }


def require_same_output_paths(original: list[str | Path], coverage: list[str | Path]) -> dict[str, Any]:
    comparison = compare_output_paths(original, coverage)
    if not comparison["sameAsOriginal"]:
        raise errors.ExecutorError(
            "COVERAGE_OUTPUT_PATH_CHANGED",
            "Coverage outputs must use the exact paths observed in the original build",
            [comparison],
        )
    return comparison


def path_within(path: str | Path, root: str | Path) -> bool:
    candidate = canonical(path, False)
    parent = canonical(root, False)
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def split_path_list(value: str) -> list[str]:
    return [part.strip().strip('"') for part in value.split(";") if part.strip()]


def verify_output_paths(args: argparse.Namespace) -> dict[str, Any]:
    """Fail before Module lookup when managed/native outputs were relocated."""
    comparison = require_same_output_paths(list(args.original_artifact), list(args.coverage_artifact))
    return envelopes.envelope("VERIFYING_OUTPUT_PATHS", "SUCCEEDED", comparison)
