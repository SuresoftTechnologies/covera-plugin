"""Read and write integrity-protected model or user decisions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cover_build.core.decision_contract import specification
from cover_build.common import fileio
from cover_build.common import jobs
from cover_build.common import pathutil
from cover_build.core import errors


DECISION_SCHEMA = "cover-build-decision/v1"


def _legacy_choice_specification(key: str, candidates: Any) -> dict[str, Any]:
    """Interpret pre-contract templates so an already waiting job can resume safely."""
    return specification(key, candidates)


def _validate_choice(key: str, value: Any, specification: Any) -> None:
    """Validate a resolved value against its explicit template contract."""
    if not isinstance(specification, dict):
        specification = _legacy_choice_specification(key, specification)
    mode = specification.get("mode")
    candidates = specification.get("candidates")
    if candidates is not None and (
        not isinstance(candidates, list) or not all(isinstance(candidate, str) for candidate in candidates)
    ):
        raise errors.ExecutorError(
            "INVALID_WORKFLOW_DECISION",
            f"Choice specification candidates for {key} must be a string array",
        )
    if mode == "ONE_OF":
        if not isinstance(value, str) or not candidates or value not in candidates:
            raise errors.ExecutorError(
                "INVALID_WORKFLOW_DECISION",
                f"Value for {key} is not one of the recorded candidates",
                candidates or [],
            )
        return
    if mode == "STRING_ARRAY_SUBSET":
        minimum = specification.get("minItems", 0)
        if not isinstance(minimum, int) or minimum < 0:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Choice specification minItems for {key} is invalid")
        if (
            not isinstance(value, list)
            or len(value) < minimum
            or not all(isinstance(item, str) and item for item in value)
            or candidates is None
            or any(item not in candidates for item in value)
        ):
            raise errors.ExecutorError(
                "INVALID_WORKFLOW_DECISION",
                f"Value for {key} must be a permitted string array",
                candidates or [],
            )
        return
    if mode == "STRING_ARRAY":
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Value for {key} must be a non-empty string array")
        return
    if mode == "NONEMPTY_STRING":
        if not isinstance(value, str) or not value:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Value for {key} must be a non-empty string")
        return
    if mode == "JSON_OBJECT":
        if not isinstance(value, dict):
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Value for {key} must be a JSON object")
        return
    if mode == "POSITIVE_INTEGER":
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Value for {key} must be a positive integer")
        return
    if mode == "EXACT_VALUE":
        if value != specification.get("value"):
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Value for {key} does not match the required value")
        return
    raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Unknown choice mode for {key}: {mode!r}")

def parse_choice(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("choice must be KEY=JSON_VALUE")
    key, encoded = value.split("=", 1)
    if not key:
        raise argparse.ArgumentTypeError("choice key cannot be empty")
    try:
        return key, json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"choice value must be JSON: {exc.msg}") from exc

def create_decision(
    job_context: str,
    template_path: str,
    owner: str,
    choices: list[tuple[str, Any]],
    escalate_to_user: bool,
    reason: str | None,
    output: str | None,
    choices_file: str | None = None,
) -> Path:
    job = jobs.load_job_context(job_context)
    template = fileio.load_json(template_path)
    if template.get("schema") != DECISION_SCHEMA:
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Expected {DECISION_SCHEMA}")
    fileio.verify_integrity(template)
    if template.get("jobId") != job["jobId"]:
        raise errors.ExecutorError("WORKFLOW_DECISION_JOB_MISMATCH", "Decision template belongs to another job")
    expected_owner = template.get("decisionOwner")
    if owner != expected_owner:
        raise errors.ExecutorError(
            "INVALID_WORKFLOW_DECISION",
            f"This decision is owned by {expected_owner}, not {owner}",
        )
    if escalate_to_user and owner != "MODEL":
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "Only a MODEL decision can escalate to USER")
    if escalate_to_user and (choices or choices_file):
        raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "An escalation cannot also resolve choices")

    combined_choices = list(choices)
    if choices_file:
        source = pathutil.canonical(choices_file)
        if not pathutil.path_within(source, job["jobDirectory"]):
            raise errors.ExecutorError(
                "INVALID_WORKFLOW_DECISION",
                "choices-file must be under the current job directory",
                [str(source)],
            )
        file_value = fileio.load_json(source)
        if not isinstance(file_value, dict):
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "choices-file must contain a JSON object")
        combined_choices.extend(file_value.items())

    allowed = template.get("choices") or {}
    resolved: dict[str, Any] = {}
    for key, value in combined_choices:
        if key in resolved:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Decision key was provided more than once: {key}")
        if key not in allowed:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"Unknown decision key: {key}", sorted(allowed))
        _validate_choice(key, value, allowed[key])
        resolved[key] = value

    action = "ESCALATE_TO_USER" if escalate_to_user else "RESOLVE"
    decision = fileio.add_integrity(
        {
            "schema": DECISION_SCHEMA,
            "jobId": job["jobId"],
            "purpose": template.get("purpose"),
            "decisionOwner": owner,
            "resolution": {"action": action, "owner": owner, "reason": reason},
            "choices": resolved,
            "template": str(pathutil.canonical(template_path)),
        }
    )
    destination = pathutil.canonical(
        output or str(Path(job["directories"]["temporary"]) / "decision.json"),
        False,
    )
    fileio.atomic_write_json(destination, decision)
    return destination

def decision_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-context", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--owner", required=True, choices=["MODEL", "USER"])
    parser.add_argument("--choice", action="append", default=[], type=parse_choice, help="KEY=JSON_VALUE; repeat as needed")
    parser.add_argument("--choices-file", help="JSON object under the current job directory; use for a structured originalBuildPlan")
    parser.add_argument("--escalate-to-user", action="store_true")
    parser.add_argument("--reason")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        destination = create_decision(
            args.job_context,
            args.template,
            args.owner,
            args.choice,
            args.escalate_to_user,
            args.reason,
            args.output,
            args.choices_file,
        )
    except errors.ExecutorError as exc:
        print(json.dumps({"status": "FAILED", "error": {"code": exc.code, "message": str(exc), "evidence": exc.evidence}}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "SUCCEEDED", "decision": str(destination)}, ensure_ascii=False, indent=2))
    return 0
