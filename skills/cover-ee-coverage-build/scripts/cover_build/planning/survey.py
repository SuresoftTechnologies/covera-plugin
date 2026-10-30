"""Create the pre-build COVER environment survey."""

from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any

from cover_build.common import fileio
from cover_build.core import errors


SURVEY_SCHEMA = "cover-environment-survey/v2"

SURVEY_FILENAME = "cover-environment-survey.json"

TOOL_CANDIDATES = (
    "cmake", "make", "gmake", "msbuild", "cl", "gcc", "g++",
    "stm32cubeidec", "arm-none-eabi-gcc", "arm-none-eabi-g++", "arm-none-eabi-ld", "arm-none-eabi-ar",
)

SENSITIVE_ARGUMENT = re.compile(r"(?i)(?:password|passwd|token|secret|authorization|api[_-]?key)")

def survey_path(job: dict[str, Any]) -> Path:
    return Path(job["directories"]["temporary"]) / SURVEY_FILENAME

def _tool_candidates() -> dict[str, str]:
    return {name: value for name in TOOL_CANDIDATES if (value := shutil.which(name))}

def _file_type_counts(discovery: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in discovery.get("data", {}).get("files", []):
        name = str(item.get("type") or "UNKNOWN")
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))

def _stm32cubeide_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": value.get("status"),
        "confidence": value.get("confidence"),
        "projects": [
            {
                "projectName": item.get("projectName"),
                "projectDirectory": item.get("projectDirectory"),
                "configurations": list(item.get("configurations") or []),
                "toolCommands": list(item.get("toolCommands") or []),
            }
            for item in value.get("projects", [])
            if isinstance(item, dict)
        ],
    }

def _evidence_file(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in ("path", "relativePath", "type", "size", "modifiedNs", "sha256", "likelyBuildEntry")
        if item.get(key) is not None
    }

def _build_evidence_summary(value: dict[str, Any], choices: dict[str, Any] | None = None) -> dict[str, Any]:
    data = value.get("data", value)
    caches = list(data.get("cmakeCaches") or [])
    scripts = [item for item in data.get("buildScripts") or [] if isinstance(item, dict)]
    original_plan = (choices or {}).get("originalBuildPlan")
    return {
        "userProvidedOriginalBuild": {
            "present": isinstance(original_plan, dict),
            "candidate": {
                "requiresValidation": True,
                "declaredDriver": original_plan.get("driver"),
                "declaredEvidenceSource": original_plan.get("evidenceSource"),
            } if isinstance(original_plan, dict) else None,
        },
        "customerBuildScripts": [_evidence_file(item) for item in scripts if item.get("type") == "BUILD_SCRIPT"],
        "ciFiles": [_evidence_file(item) for item in scripts if item.get("type") == "CI_SCRIPT"],
        "cmakePresets": list(data.get("cmakePresets") or []),
        "cmakeCaches": caches,
        "existingBuildLogs": [_evidence_file(item) for item in data.get("existingBuildLogs") or [] if isinstance(item, dict)],
        "makeInjection": [
            {
                "path": item.get("relativePath") or Path(str(item.get("path") or "")).name,
                "status": item.get("status"),
                "nonstandardToolVariables": item.get("nonstandardToolVariables", {}),
                "hardcodedToolRoles": item.get("hardcodedToolRoles", []),
            }
            for item in data.get("makeInjection") or []
        ],
    }

def create_pre_build_survey(
    job: dict[str, Any],
    discovery: dict[str, Any],
    stm32cubeide_discovery: dict[str, Any],
    build_evidence: dict[str, Any],
    choices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record non-sensitive evidence used to choose an original build method."""
    agent_root = os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    agent_path = Path(agent_root).expanduser() if agent_root else None
    blockers: list[str] = []
    if not job.get("serverOrigin"):
        blockers.append("SERVER_URL_NOT_CONFIGURED")
    if agent_path is None or not agent_path.is_dir():
        blockers.append("AGENT_INSTALLATION_NOT_FOUND")
    return fileio.add_integrity(
        {
            "schema": SURVEY_SCHEMA,
            "jobId": job["jobId"],
            "projectRoot": job["projectRoot"],
            "createdAt": fileio.utc_now(),
            "updatedAt": fileio.utc_now(),
            "phase": "ENVIRONMENT_SURVEY_CREATED",
            "purpose": "SELECT_ORIGINAL_BUILD_METHOD",
            "host": {
                "os": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "architecture": platform.machine(),
            },
            "coverEnvironment": {
                "serverUrlConfigured": bool(job.get("serverOrigin")),
                "agentInstallationConfigured": agent_path is not None,
                "agentInstallationExists": bool(agent_path and agent_path.is_dir()),
                "plannedMonitoringDirectory": str(agent_path / "coverage") if agent_path else None,
            },
            "project": {
                "candidateLanguages": dict(discovery.get("data", {}).get("projectLanguages") or {}),
                "candidateBuildSystems": list(discovery.get("data", {}).get("candidateBuildSystems") or []),
                "unsupportedBuildSystems": list(discovery.get("data", {}).get("unsupportedBuildSystems") or []),
                "discoveredFileTypes": _file_type_counts(discovery),
                "toolCandidates": _tool_candidates(),
            },
            "stm32CubeIDE": _stm32cubeide_summary(stm32cubeide_discovery),
            "buildEvidence": _build_evidence_summary(build_evidence, choices),
            "originalBuildDecision": {
                "status": "PENDING",
                "selection": None,
            },
            "readiness": {"blockers": blockers},
        }
    )

def _safe_command(value: dict[str, Any]) -> dict[str, Any]:
    """Keep command evidence while preventing credential-like arguments from persistence."""
    result = {key: value.get(key) for key in ("executable", "workingDirectory") if key in value}
    arguments = value.get("arguments")
    if isinstance(arguments, list):
        result["arguments"] = ["[REDACTED]" if SENSITIVE_ARGUMENT.search(str(item)) else str(item) for item in arguments]
    return result

def _plan_selection(plan: dict[str, Any]) -> dict[str, Any]:
    commands = plan.get("commands") if isinstance(plan.get("commands"), dict) else {}
    structured_commands = {
        phase: _safe_command(command)
        for phase, command in commands.items()
        if isinstance(command, dict)
    }
    working_directory = plan.get("originalBuildDirectory")
    if not working_directory and structured_commands:
        working_directory = next(iter(structured_commands.values())).get("workingDirectory")
    environment_initialization = plan.get("environmentInitialization")
    if not isinstance(environment_initialization, dict):
        environment_initialization = None
    return {
        "driver": plan.get("driver"),
        "entry": plan.get("adapterEntry"),
        "evidenceSource": plan.get("evidenceSource"),
        "workingDirectory": working_directory,
        "environmentInitialization": environment_initialization,
        "environmentVariableNames": sorted(str(name) for name in (plan.get("environmentDelta") or {})),
        "structuredCommands": structured_commands,
        "configuration": plan.get("configuration"),
        "targets": list(plan.get("targets") or []),
        "evidence": list(plan.get("evidence") or []),
    }

def finalize_original_build_selection(survey: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Freeze the selected method before executing the original build."""
    result = dict(survey)
    result["phase"] = "ORIGINAL_BUILD_METHOD_SELECTED"
    result["updatedAt"] = fileio.utc_now()
    result["originalBuildDecision"] = {
        "status": "SELECTED",
        "selection": _plan_selection(plan),
    }
    return fileio.add_integrity(result)

def write_survey(path: Path, value: dict[str, Any]) -> None:
    fileio.atomic_write_json(path, value)

def load_survey(path: Path) -> dict[str, Any]:
    value = fileio.load_json(path)
    if value.get("schema") != SURVEY_SCHEMA:
        raise errors.ExecutorError("INVALID_ENVIRONMENT_SURVEY", f"Expected {SURVEY_SCHEMA}")
    fileio.verify_integrity(value)
    return value
