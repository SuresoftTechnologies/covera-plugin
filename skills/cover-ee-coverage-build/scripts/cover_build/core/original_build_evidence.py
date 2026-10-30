from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cover_build.common import fileio
from cover_build.common import pathutil


SCHEMA = "cover-original-build-evidence/v1"
FILENAME = "original-build-evidence.json"
SENSITIVE_ARGUMENT = re.compile(r"(?i)(?:password|passwd|token|secret|authorization|api[_-]?key)")


def path(job: dict[str, Any]) -> Path:
    return Path(job["directories"]["temporary"]) / FILENAME


def _safe_command(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value.get(key)
        for key in ("executable", "workingDirectory", "exitCode", "startedAt", "finishedAt", "log")
        if value.get(key) is not None
    }
    arguments = value.get("arguments")
    if isinstance(arguments, list):
        result["arguments"] = [
            "[REDACTED]" if SENSITIVE_ARGUMENT.search(str(item)) else str(item)
            for item in arguments
        ]
    return result


def create(job: dict[str, Any], native_build: dict[str, Any]) -> dict[str, Any]:
    data = native_build.get("data", native_build)
    plan = data.get("originalBuildPlan") if isinstance(data.get("originalBuildPlan"), dict) else {}
    commands = data.get("commands") if isinstance(data.get("commands"), dict) else {}
    toolchain = data.get("toolchain") if isinstance(data.get("toolchain"), dict) else {}
    artifacts = list(data.get("artifacts") or [])
    if data.get("artifact") and not artifacts:
        artifacts = [{"path": data["artifact"]}]
    return fileio.add_integrity(
        {
            "schema": SCHEMA,
            "jobId": job["jobId"],
            "capturedAt": fileio.utc_now(),
            "driver": data.get("driver"),
            "entry": data.get("entry"),
            "buildDirectory": data.get("buildDirectory") or plan.get("originalBuildDirectory"),
            "configuration": data.get("configuration") or plan.get("configuration"),
            "platform": data.get("platform"),
            "selectionEvidenceSource": plan.get("evidenceSource"),
            "environmentInitialization": plan.get("environmentInitialization"),
            "executedCommands": {
                phase: _safe_command(command)
                for phase, command in commands.items()
                if isinstance(command, dict)
            },
            "cmakeReset": data.get("cmakeReset"),
            "diagnosticLog": data.get("diagnosticLog"),
            "diagnosticLogEvidence": data.get("diagnosticLogEvidence"),
            "actualObservedToolchain": {
                "family": toolchain.get("family"),
                "observation": toolchain.get("observation") or {
                    "kind": "NATIVE" if toolchain.get("family") not in {"CSHARP", "JAVA"} else toolchain.get("family"),
                    "tools": dict(toolchain.get("tools") or {}),
                },
                "tools": dict(toolchain.get("tools") or {}),
                "evidence": dict(toolchain.get("evidence") or {}),
                "unusedRoles": list(toolchain.get("unusedRoles") or []),
                "requestedRolesNotObserved": list(toolchain.get("requestedRolesNotObserved") or []),
            },
            "artifacts": artifacts,
            "originalOutputPaths": [
                str(pathutil.canonical(item["path"], False))
                for item in artifacts
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ],
            "artifactVerification": data.get("artifactVerification"),
        }
    )


def write(job: dict[str, Any], native_build: dict[str, Any]) -> Path:
    output = path(job)
    fileio.atomic_write_json(output, create(job, native_build))
    return output
