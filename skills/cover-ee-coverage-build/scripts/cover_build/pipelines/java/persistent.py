"""Reusable Gradle setup: JACOV JAR and init script published beside the project."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import project_edit
from cover_build.core import errors
from cover_build.core import persistent_setup


def _write_manual_asset(destination: Path, contents: bytes, previous_sha: str | None = None,
                        changes: list | None = None) -> dict[str, Any]:
    """Create a manual COVER file without overwriting a customer-owned file."""
    if destination.exists():
        if not destination.is_file() or (destination.read_bytes() != contents and
                (not previous_sha or pathutil.sha256_file(destination).casefold() != previous_sha.casefold())):
            raise errors.ExecutorError(
                "PERSISTENT_MANUAL_ASSET_CONFLICT",
                "A different file already occupies a COVER manual setup path",
                [str(destination)],
            )
    if not destination.exists() or destination.read_bytes() != contents:
        previous = destination.read_bytes() if destination.exists() else None
        temporary = destination.with_name(destination.name + ".cover-ee-tmp")
        temporary.write_bytes(contents)
        os.replace(temporary, destination)
        if changes is not None:
            changes.append((destination, previous, hashlib.sha256(contents).hexdigest()))
    return {
        "source": "COVER_MANUAL_SETUP",
        "path": str(destination),
        "size": destination.stat().st_size,
        "sha256": pathutil.sha256_file(destination),
    }


def _assert_manual_asset_available(destination: Path, contents: bytes, previous_sha: str | None = None) -> None:
    if destination.exists() and (
        not destination.is_file() or (destination.read_bytes() != contents and
        (not previous_sha or pathutil.sha256_file(destination).casefold() != previous_sha.casefold()))
    ):
        raise errors.ExecutorError(
            "PERSISTENT_MANUAL_ASSET_CONFLICT",
            "A different file already occupies a COVER manual setup path",
            [str(destination)],
        )


def _publish_gradle_manual_assets(
    config_assets: list[dict[str, Any]],
    details: dict[str, Any],
    changes: list | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Mirror the product manual: plugin JAR + init script in project root."""
    project_root = Path(details["projectRoot"]).resolve(strict=True)
    init = next(
        (item for item in config_assets if Path(item["source"]).name == "jacov-gradle-init.gradle"),
        None,
    )
    plugin = next(
        (item for item in config_assets if Path(item["source"]).name.startswith("jacov-gradle-plugin-") and Path(item["source"]).suffix == ".jar"),
        None,
    )
    if init is None or plugin is None:
        raise errors.ExecutorError(
            "PERSISTENT_GRADLE_ASSETS_MISSING",
            "Gradle manual setup requires the selected JACOV plugin JAR and init script",
        )

    plugin_source = Path(plugin["path"])
    plugin_destination = project_root / Path(plugin["source"]).name
    plugin_contents = plugin_source.read_bytes()

    init_source = Path(init["path"])
    contents = init_source.read_text(encoding="utf-8-sig")
    plugin_paths = {
        str(Path(plugin["source"]).resolve()): str(plugin_destination),
        str(plugin_source.resolve()): str(plugin_destination),
    }
    contents = project_edit._replace_paths(contents, plugin_paths)
    init_destination = project_root / "jacov-gradle-init.gradle"
    init_contents = contents.encode("utf-8")
    previous = fileio.load_json(persistent_setup.state_path(project_root)) if persistent_setup.state_path(project_root).is_file() else {}
    owned = {str(Path(item["path"]).resolve()): item.get("sha256") for item in previous.get("assets", [])
             if item.get("source") == "COVER_MANUAL_SETUP" and item.get("path")}
    plugin_sha, init_sha = owned.get(str(plugin_destination)), owned.get(str(init_destination))
    _assert_manual_asset_available(plugin_destination, plugin_contents, plugin_sha)
    _assert_manual_asset_available(init_destination, init_contents, init_sha)
    plugin_record = _write_manual_asset(plugin_destination, plugin_contents, plugin_sha, changes)
    init_record = _write_manual_asset(init_destination, init_contents, init_sha, changes)
    return [plugin_record, init_record], {
        str(Path(init["source"]).resolve()): str(init_destination),
        str(init_source.resolve()): str(init_destination),
    }
