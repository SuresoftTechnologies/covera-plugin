"""Shared original-build and artifact helpers for managed languages."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any


from cover_build.core.errors import WorkflowWaiting
from cover_build.planning import build_plan
from cover_build.common import open_api
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors


def _absolute_entry(root: Path, relative: str) -> Path:
    path = pathutil.canonical(root / relative, False)
    if not path.is_file() or not pathutil.path_within(path, root):
        raise errors.ExecutorError("INVALID_MANAGED_ENTRY", "Managed build entry is outside the project or missing", [relative])
    return path

def _select(name: str, values: list[str], choices: dict[str, Any]) -> str:
    supplied = choices.get(name)
    if supplied is not None:
        if supplied not in values:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"{name} is not a candidate", values)
        return str(supplied)
    if len(values) == 1:
        return values[0]
    raise WorkflowWaiting(
        "MANAGED_BUILD_SELECTION_REQUIRED",
        f"Select one {name}",
        {name: values},
        decision_owner="MODEL",
        purpose="BUILD_ENVIRONMENT",
        evidence=values,
    )

def _executable(name: str, explicit: str | None = None) -> Path:
    candidate = explicit or shutil.which(name)
    if not candidate:
        raise WorkflowWaiting(
            "MANAGED_BUILD_TOOL_NOT_FOUND",
            f"No executable was found for {name}",
            {"managedBuildTool": []},
            decision_owner="USER",
            purpose="CUSTOMER_CONFIGURATION",
        )
    return pathutil.canonical(candidate)

def _project_wrapper(entry: Path, project_root: Path, names: tuple[str, ...]) -> Path | None:
    """Prefer a customer-owned wrapper located beside or above the selected entry."""
    root = entry.parent
    project_root = pathutil.canonical(project_root)
    directories = [root, *(parent for parent in root.parents if pathutil.path_within(parent, project_root))]
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return pathutil.canonical(candidate)
    return None

def _command_spec(executable: Path, arguments: list[str], cwd: Path, *, coverage: bool = False, environment_delta: dict[str, str] | None = None) -> dict[str, Any]:
    if any(re.search(r"(?i)(?:password|token|secret|api[_-]?key)", value) for value in arguments):
        raise errors.ExecutorError("SENSITIVE_OR_INVALID_BUILD_COMMAND", "Managed build arguments contain credential-like data")
    return {
        "executable": str(executable),
        "arguments": arguments,
        "workingDirectory": str(cwd),
        "environmentMode": "inherit",
        "environmentDelta": dict(environment_delta or {}),
        "timeoutSeconds": 3600,
        "coverToolEnvironment": coverage,
    }

def _run(spec: dict[str, Any], log: Path, code: str) -> dict[str, Any]:
    result = processes.run_command(spec, log)
    if result["exitCode"] != 0:
        raise errors.ExecutorError(code, f"Managed build exited with {result['exitCode']}", [result["log"]])
    result.pop("outputTail", None)
    return result

def _paths_from_log(log: Path, root: Path, suffixes: set[str]) -> list[Path]:
    patterns = (
        re.compile(r"COVER_OBS_ARTIFACT=(.+\.(?:jar|war))$"),
        re.compile(r"(?:TargetPath\s*=|->)\s*[\"']?([^\"'\r\n]+?\.(?:dll|exe|jar|war))(?=\s|[\"']|$)", re.IGNORECASE),
        re.compile(r"Building\s+(?:jar|war):\s*[\"']?([^\"'\r\n]+?\.(?:jar|war))(?=\s|[\"']|$)", re.IGNORECASE),
    )
    results: list[Path] = []
    for line in processes.iter_output_lines(log):
        for pattern in patterns:
            for raw in pattern.findall(line):
                candidate = Path(str(raw).strip())
                candidate = candidate if candidate.is_absolute() else root / candidate
                if candidate.suffix.casefold() not in suffixes or not candidate.is_file():
                    continue
                resolved = pathutil.canonical(candidate)
                if not pathutil.path_within(resolved, root):
                    continue
                if not any(pathutil.same_path(resolved, existing) for existing in results):
                    results.append(resolved)
    return results

def _resolve_artifacts(
    job: dict[str, Any],
    choices: dict[str, Any],
    command_result: dict[str, Any],
    suffixes: set[str],
) -> list[Path]:
    explicit = choices.get("expectedArtifacts")
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "expectedArtifacts must be a non-empty array")
        artifacts = [pathutil.canonical(value, False) for value in explicit]
    else:
        root = Path(job["projectRoot"])
        artifacts = _paths_from_log(Path(command_result["log"]), root, suffixes)
    artifacts = [path for path in artifacts if path.is_file() and pathutil.path_within(path, job["projectRoot"])]
    if not artifacts:
        raise WorkflowWaiting(
            "MANAGED_OUTPUT_REQUIRED",
            "The original managed build did not resolve its final output paths",
            {"expectedArtifacts": []},
            decision_owner="MODEL",
            purpose="BUILD_OUTPUT",
            evidence=[command_result["log"]],
        )
    return artifacts

def _managed_plan(
    job: dict[str, Any], driver: str, executable: Path, entry: Path, specification: dict[str, Any],
    configuration: str | None, targets: list[str], frameworks: list[str], injection: str,
    *, restore_specification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commands = {"build": specification}
    if restore_specification is not None:
        commands["restore"] = restore_specification
    return build_plan.validate_original_build_plan(job, {
        "schema": build_plan.ORIGINAL_BUILD_PLAN_SCHEMA,
        "jobId": job["jobId"],
        "origin": "MODEL_INFERRED",
        "evidenceSource": "MODEL",
        "environmentInitialization": {"method": "INHERITED_PROCESS"},
        "driver": driver,
        "buildTool": str(executable),
        "adapterEntry": str(entry),
        "originalBuildDirectory": specification["workingDirectory"],
        "configuration": configuration,
        "targets": targets,
        "requiredRoles": [],
        "targetTools": {},
        "targetFrameworks": frameworks,
        "commands": commands,
        "coverageInjection": {"strategy": injection},
        "expectedArtifacts": [],
        "evidence": [str(entry)],
    }, driver)

def _provided_plan(
    job: dict[str, Any], choices: dict[str, Any], allowed_drivers: set[str]
) -> dict[str, Any] | None:
    raw = choices.get("originalBuildPlan")
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("driver") not in allowed_drivers:
        raise errors.ExecutorError(
            "INVALID_ORIGINAL_BUILD_PLAN",
            "The supplied originalBuildPlan does not match the selected managed adapter",
            sorted(allowed_drivers),
        )
    return build_plan.validate_original_build_plan(job, raw, str(raw["driver"]))

def _coverage_artifacts(
    job: dict[str, Any], command_result: dict[str, Any], suffixes: set[str], expected: list[Path]
) -> list[Path]:
    artifacts = _paths_from_log(Path(command_result["log"]), Path(job["projectRoot"]), suffixes)
    if artifacts:
        return artifacts
    started = open_api.parse_server_datetime(command_result["startedAt"]).timestamp() - 2.0
    finished = open_api.parse_server_datetime(command_result["finishedAt"]).timestamp() + 2.0
    fresh_expected = [
        path for path in expected
        if path.is_file() and started <= path.stat().st_mtime <= finished
    ]
    if len(fresh_expected) == len(expected):
        return fresh_expected
    raise errors.ExecutorError(
        "COVERAGE_OUTPUT_PATH_NOT_OBSERVED",
        "The coverage build neither logged nor freshly produced every original output path",
        [command_result["log"], *[str(path) for path in expected]],
    )
