"""Project-local cache for previously verified build-environment discovery."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import project_index
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.core import errors


SCHEMA = "cover-build-environment-profile/v1"
PROFILE_VERSION = "1"
PROFILE_NAME = "environment-profile.json"
SENSITIVE_NAME = re.compile(
    r"(?i)(?:password|passwd|token|secret|authorization|cookie|api[_-]?key|access[_-]?key)"
)


def path(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / ".cover-build" / "cache" / PROFILE_NAME


def _is_build_definition(relative: Path) -> bool:
    name = relative.name.casefold()
    return (
        name in project_index.BUILD_NAMES
        or relative.suffix.casefold() in project_index.BUILD_SUFFIXES
        or (
            len(relative.parts) == 3
            and tuple(part.casefold() for part in relative.parts[:2]) == (".github", "workflows")
            and relative.suffix.casefold() in {".yml", ".yaml"}
        )
    )


def build_definition_inventory(project_root: str | Path) -> list[dict[str, Any]]:
    """Hash only build definitions, not the whole source tree."""
    root = Path(project_root).resolve(strict=True)
    records: list[dict[str, Any]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in sorted(dirnames, key=str.casefold)
            if name.casefold() not in project_index.SKIP_DIRS
        ]
        current = Path(directory)
        for filename in sorted(filenames, key=str.casefold):
            candidate = current / filename
            relative = candidate.relative_to(root)
            if not _is_build_definition(relative):
                continue
            stat = candidate.stat()
            records.append(
                {
                    "relativePath": relative.as_posix(),
                    "size": stat.st_size,
                    "sha256": pathutil.sha256_file(candidate),
                }
            )
    return records


def _file_identity(raw: str | None) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_file():
        return None
    resolved = candidate.resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size": stat.st_size,
        "sha256": pathutil.sha256_file(resolved),
    }


def _tool_identities(original_evidence: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[str] = []
    observed = original_evidence.get("actualObservedToolchain")
    if isinstance(observed, dict):
        tools = observed.get("tools")
        if isinstance(tools, dict):
            candidates.extend(value for value in tools.values() if isinstance(value, str))
    commands = original_evidence.get("executedCommands")
    if isinstance(commands, dict):
        for command in commands.values():
            if isinstance(command, dict) and isinstance(command.get("executable"), str):
                candidates.append(command["executable"])
    initialization = original_evidence.get("environmentInitialization")
    if isinstance(initialization, dict) and isinstance(initialization.get("scriptPath"), str):
        candidates.append(initialization["scriptPath"])
    identities = [_file_identity(value) for value in candidates]
    unique = {item["path"].casefold(): item for item in identities if item is not None}
    return sorted(unique.values(), key=lambda item: item["path"].casefold())


def _reject_sensitive(value: Any, location: str = "choices") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or SENSITIVE_NAME.search(key):
                raise errors.ExecutorError(
                    "SENSITIVE_ENVIRONMENT_PROFILE",
                    "Environment profiles cannot persist credential-like fields",
                    [f"{location}.{key}"],
                )
            _reject_sensitive(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive(child, f"{location}[{index}]")


def _conflicts(cached: dict[str, Any], requested: dict[str, Any]) -> bool:
    return any(key in cached and cached[key] != value for key, value in requested.items())


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def request_signature(args: Any) -> dict[str, Any]:
    """Keep only CLI values that can select a different build environment."""
    names = (
        "artifact",
        "target",
        "gcc_build_tool",
        "gcc_build_directory",
        "gcc_generator",
        "gcc_configuration",
        "gcc_configure_arg",
        "gcc_clean_target",
        "gcc_cover_config",
        "stm32cubeide_executable",
        "vs_real_tool",
    )
    return _json_safe({name: getattr(args, name, None) for name in names})


def _rebind_choices(choices: dict[str, Any], job_id: str) -> dict[str, Any]:
    rebound = dict(choices)
    original_plan = rebound.get("originalBuildPlan")
    if isinstance(original_plan, dict):
        rebound["originalBuildPlan"] = fileio.add_integrity(
            {key: value for key, value in original_plan.items() if key != "integrity"}
            | {"jobId": job_id}
        )
    return rebound


def publish(
    job: dict[str, Any],
    *,
    choices: dict[str, Any],
    request: dict[str, Any],
    project_index_value: dict[str, Any],
    managed_evidence: dict[str, Any],
    discovery: dict[str, Any],
    stm32cubeide_discovery: dict[str, Any],
) -> Path:
    """Publish only after the complete coverage workflow succeeds."""
    _reject_sensitive(choices)
    original_path = Path(job["directories"]["temporary"]) / "original-build-evidence.json"
    original_evidence = fileio.load_json(original_path)
    profile = fileio.add_integrity(
        {
            "schema": SCHEMA,
            "profileVersion": PROFILE_VERSION,
            "projectRoot": str(Path(job["projectRoot"]).resolve()),
            "sourceJobId": job["jobId"],
            "verifiedAt": fileio.utc_now(),
            "buildDefinitions": build_definition_inventory(job["projectRoot"]),
            "toolIdentities": _tool_identities(original_evidence),
            "choices": choices,
            "request": request,
            "projectIndex": project_index_value,
            "managedEvidence": managed_evidence,
            "discoveryData": discovery.get("data", discovery),
            "stm32CubeIDEDiscovery": stm32cubeide_discovery,
        }
    )
    output = path(job["projectRoot"])
    fileio.atomic_write_json(output, profile)
    return output


def load(
    project_root: str | Path,
    requested_choices: dict[str, Any],
    *,
    request: dict[str, Any],
    job_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Return a compatible profile and a stable hit/miss reason."""
    profile_path = path(project_root)
    if not profile_path.is_file():
        return None, "NOT_FOUND"
    try:
        profile = fileio.load_json(profile_path)
        if profile.get("schema") != SCHEMA or profile.get("profileVersion") != PROFILE_VERSION:
            return None, "SCHEMA_CHANGED"
        fileio.verify_integrity(profile)
        if Path(str(profile.get("projectRoot") or "")).resolve() != Path(project_root).resolve():
            return None, "PROJECT_ROOT_CHANGED"
        cached_choices = profile.get("choices")
        if not isinstance(cached_choices, dict):
            return None, "CHOICES_INVALID"
        _reject_sensitive(cached_choices)
        _reject_sensitive(requested_choices, "requestedChoices")
        if _conflicts(cached_choices, requested_choices):
            return None, "REQUEST_CHANGED"
        if profile.get("request") != request:
            return None, "REQUEST_CHANGED"
        if profile.get("buildDefinitions") != build_definition_inventory(project_root):
            return None, "BUILD_DEFINITIONS_CHANGED"
        for identity in profile.get("toolIdentities") or []:
            if not isinstance(identity, dict) or _file_identity(identity.get("path")) != identity:
                return None, "TOOL_IDENTITY_CHANGED"
        if not all(
            isinstance(profile.get(name), dict)
            for name in ("projectIndex", "managedEvidence", "discoveryData", "stm32CubeIDEDiscovery")
        ):
            return None, "EVIDENCE_INVALID"
        return {**profile, "choices": _rebind_choices(cached_choices, job_id)}, "HIT"
    except (OSError, ValueError, errors.ExecutorError):
        return None, "PROFILE_INVALID"
