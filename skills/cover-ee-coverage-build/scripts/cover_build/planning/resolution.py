"""Validate resumable decisions and produce model or user waiting state."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from cover_build.core import build_reporting
from cover_build.core.decision_contract import specification
from cover_build.core.errors import WorkflowWaiting
from cover_build.common.pathutil import canonical
from cover_build.common.fileio import write_json
from cover_build.core.job_evidence import WORKFLOW_SCHEMA
from cover_build.planning import build_plan
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.core import errors


DECISION_SCHEMA = "cover-build-decision/v1"
DECISION_HISTORY_SCHEMA = "cover-build-decision-history/v1"
MODEL_FALLBACK_PURPOSES = {"BUILD_ENVIRONMENT", "ORIGINAL_BUILD_COMMAND", "BUILD_OUTPUT", "TOOLCHAIN_INFO"}
RESOLUTION_LADDER = ["SCRIPT", "MODEL", "USER"]


def fallback_source(choices: dict[str, Any]) -> str | None:
    value = choices.get("originalBuildCommandSource")
    if value is None:
        return None
    if value not in {"MODEL_INFERRED", "USER_CONFIRMED_FALLBACK"}:
        raise errors.ExecutorError(
            "INVALID_MODEL_BUILD_COMMAND",
            "originalBuildCommandSource must identify the model or user fallback tier",
            [value],
        )
    return value

def resolve_fallback_artifact(job: dict[str, Any], value: str) -> Path:
    artifact = canonical(value, False)
    if not (
        pathutil.path_within(artifact, job["projectRoot"])
        or pathutil.path_within(artifact, job["jobDirectory"])
    ):
        raise errors.ExecutorError(
            "INVALID_MODEL_BUILD_COMMAND",
            "A fallback artifact must be under projectRoot or the current job directory",
            [str(artifact)],
        )
    return artifact

def load_decision(path: str | None, job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path:
        return {}, {}
    value = fileio.load_json(path)
    if value.get("schema") != DECISION_SCHEMA:
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Expected {DECISION_SCHEMA}")
    fileio.verify_integrity(value)
    if value.get("jobId") != job["jobId"]:
        raise errors.ExecutorError("WORKFLOW_DECISION_JOB_MISMATCH", "Decision file belongs to another job")
    choices = value.get("choices")
    if not isinstance(choices, dict):
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "choices must be an object")
    resolution = value.get("resolution") or {"action": "RESOLVE", "owner": value.get("decisionOwner")}
    if not isinstance(resolution, dict) or resolution.get("action") not in {"RESOLVE", "ESCALATE_TO_USER"}:
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "resolution.action must be RESOLVE or ESCALATE_TO_USER")
    if resolution.get("action") == "ESCALATE_TO_USER" and resolution.get("owner") != "MODEL":
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "Only the model can escalate its unresolved decision to the user")
    return choices, resolution

def load_decision_history(job: dict[str, Any]) -> dict[str, Any]:
    path = Path(job["directories"]["temporary"]) / "decision-history.json"
    if not path.is_file():
        return {
            "schema": DECISION_HISTORY_SCHEMA,
            "jobId": job["jobId"],
            "choices": {},
            "decisions": [],
        }
    history = fileio.load_json(path)
    if history.get("schema") != DECISION_HISTORY_SCHEMA:
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION_HISTORY", f"Expected {DECISION_HISTORY_SCHEMA}")
    fileio.verify_integrity(history)
    if history.get("jobId") != job["jobId"]:
        raise errors.ExecutorError("WORKFLOW_DECISION_JOB_MISMATCH", "Decision history belongs to another job")
    if not isinstance(history.get("choices"), dict) or not isinstance(history.get("decisions"), list):
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION_HISTORY", "Decision history has invalid choices or decisions")
    reject_sensitive_choice_keys(history["choices"])
    return history

def reject_sensitive_choice_keys(value: Any, path: str = "choices") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or build_plan.SENSITIVE_NAME.search(key):
                raise errors.ExecutorError(
                    "SENSITIVE_WORKFLOW_DECISION",
                    "Decision history cannot contain credential-like fields",
                    [f"{path}.{key}"],
                )
            reject_sensitive_choice_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive_choice_keys(child, f"{path}[{index}]")

def load_resumable_decision(path: str | None, job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge job-scoped resolved choices so sequential decisions survive resume."""
    history = load_decision_history(job)
    current_choices, resolution = load_decision(path, job)
    merged_choices = {**history["choices"], **current_choices}
    reject_sensitive_choice_keys(merged_choices)
    if path and resolution.get("action") == "RESOLVE":
        decision = fileio.load_json(path)
        decisions = list(history["decisions"])
        decision_record = {
            "purpose": decision.get("purpose"),
            "owner": decision.get("decisionOwner"),
            "decisionIntegrity": decision.get("integrity"),
        }
        if decision_record not in decisions:
            decisions.append(decision_record)
        persisted = fileio.add_integrity(
            {
                "schema": DECISION_HISTORY_SCHEMA,
                "jobId": job["jobId"],
                "choices": merged_choices,
                "decisions": decisions,
            }
        )
        write_json(Path(job["directories"]["temporary"]) / "decision-history.json", persisted)
    return merged_choices, resolution

def apply_model_fallback(exc: WorkflowWaiting, resolution: dict[str, Any]) -> WorkflowWaiting:
    """Convert a model-owned unresolved result into the final user fallback."""
    if resolution.get("action") != "ESCALATE_TO_USER":
        return exc
    if exc.decision_owner != "MODEL" or exc.purpose not in MODEL_FALLBACK_PURPOSES:
        raise errors.ExecutorError(
            "INVALID_WORKFLOW_DECISION",
            "This stage cannot be escalated from the model to the user",
            [exc.decision_owner, exc.purpose],
        )
    reason = str(resolution.get("reason") or "The model could not resolve the recorded evidence")
    choices = dict(exc.choices)
    if "originalBuildCommandSource" in choices:
        choices["originalBuildCommandSource"] = ["USER_CONFIRMED_FALLBACK"]
    return WorkflowWaiting(
        exc.code,
        exc.message,
        choices,
        decision_owner="USER",
        purpose=exc.purpose,
        evidence=[*exc.evidence, {"modelEscalationReason": reason}],
    )


def _choice_specifications(choices: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Turn a waiting state's candidates into explicit, type-safe decision contracts."""
    return {key: specification(key, candidates) for key, candidates in choices.items()}


def waiting(job: dict[str, Any], stages: list[dict[str, Any]], exc: WorkflowWaiting) -> dict[str, Any]:
    status = "WAITING_MODEL" if exc.decision_owner == "MODEL" else "WAITING_USER"
    state = "WAITING_MODEL_DECISION" if exc.decision_owner == "MODEL" else "WAITING_USER_INPUT"
    fallback_owner = "USER" if exc.decision_owner == "MODEL" and exc.purpose in MODEL_FALLBACK_PURPOSES else None
    attempted_by = ["SCRIPT"] if exc.decision_owner == "MODEL" else ["SCRIPT", "MODEL"] if exc.purpose in MODEL_FALLBACK_PURPOSES else ["SCRIPT"]
    template = fileio.add_integrity(
        {
            "schema": DECISION_SCHEMA,
            "jobId": job["jobId"],
            "state": state,
            "decisionOwner": exc.decision_owner,
            "fallbackOwner": fallback_owner,
            "purpose": exc.purpose,
            "resolutionLadder": RESOLUTION_LADDER if exc.purpose in MODEL_FALLBACK_PURPOSES else ["SCRIPT", "USER"],
            "attemptedBy": attempted_by,
            "resolution": {"action": "RESOLVE", "owner": exc.decision_owner, "reason": None},
            "choices": _choice_specifications(exc.choices),
            "evidence": exc.evidence,
            "instructions": "Use cover_workflow_runner.py decision to create decision.json, then resume the same job.json with --decision decision.json.",
        }
    )
    decision_path = Path(job["directories"]["temporary"]) / "decision.template.json"
    write_json(decision_path, template)
    result = {
        "schema": WORKFLOW_SCHEMA,
        "jobId": job["jobId"],
        "status": status,
        "state": state,
        "decisionOwner": exc.decision_owner,
        "fallbackOwner": fallback_owner,
        "purpose": exc.purpose,
        "resolutionLadder": template["resolutionLadder"],
        "attemptedBy": attempted_by,
        "reason": {"code": exc.code, "message": exc.message},
        "decisionTemplate": str(decision_path),
        "stages": stages,
    }
    build_reporting.attach(job, result)
    write_json(Path(job["jobDirectory"]) / "workflow-state.json", result)
    return result
