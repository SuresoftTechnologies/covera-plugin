"""Persist stage state and job-local evidence."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import project_index

from cover_build.core import original_build_evidence, state_store
from cover_build.common.fileio import write_json
from cover_build.planning import survey
from cover_build.common import envelopes
from cover_build.common import fileio


WORKFLOW_SCHEMA = "cover-build-workflow/v1"
_JOB_PROGRESS_CLOCKS: dict[str, tuple[float, float]] = {}
_JOB_PROGRESS_UTC: dict[str, str] = {}


def stage_path(job: dict[str, Any], number: int, name: str) -> Path:
    directories = job.get("directories", {})
    stages = directories.get("stages")
    if stages:
        root = Path(stages)
    elif job.get("jobDirectory"):
        root = Path(job["jobDirectory"]) / "stages"
    elif directories.get("temporary"):
        root = Path(directories["temporary"]).parent / "stages"
    else:
        root = Path(job["projectRoot"]) / ".cover-build" / "jobs" / str(job.get("jobId", "current")) / "stages"
    return root / f"{number:02d}-{name}.json"

def job_work_path(job: dict[str, Any], directory: str, filename: str) -> Path:
    directories = job.get("directories", {})
    configured = directories.get(directory)
    if configured:
        return Path(configured) / filename
    if job.get("jobDirectory"):
        return Path(job["jobDirectory"]) / directory / filename
    if directories.get("stages"):
        return Path(directories["stages"]) / filename
    return Path(job["projectRoot"]) / ".cover-build" / "jobs" / str(job.get("jobId", "current")) / directory / filename

def job_project_input_fingerprint(job: dict[str, Any]) -> str | None:
    path = job_work_path(job, "temporary", "project-index.json")
    if not path.is_file():
        return None
    return project_index.input_fingerprint(fileio.load_json(path))

def finalize_environment_survey_plan(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    plan: dict[str, Any],
) -> dict[str, Any]:
    path = job_work_path(job, "temporary", survey.SURVEY_FILENAME)
    if not path.is_file():
        # Direct unit calls exercise adapters without main()'s discovery stages.
        # A real workflow always creates the pre-build survey before this point.
        return envelopes.envelope(
            "UPDATING_COVER_ENVIRONMENT_SURVEY",
            "SKIPPED",
            {"path": str(path), "reason": "PRE_BUILD_SURVEY_NOT_CREATED"},
        )
    def finalize() -> dict[str, Any]:
        current = survey.load_survey(path)
        updated = survey.finalize_original_build_selection(current, plan)
        survey.write_survey(path, updated)
        return envelopes.envelope("FINALIZING_COVER_ENVIRONMENT_SURVEY", "SUCCEEDED",
                                 {"path": str(path), "phase": updated["phase"]})
    return state_store.run_checkpointed(job, stages, record, 4, "finalize-cover-environment-survey",
        {"plan": plan, "projectInput": job_project_input_fingerprint(job)}, finalize,
        validate=lambda _: survey.load_survey(path).get("phase") == "ORIGINAL_BUILD_METHOD_SELECTED")

def write_original_build_evidence(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    native_build: dict[str, Any],
) -> dict[str, Any]:
    path = original_build_evidence.write(job, native_build)
    return record(
        job,
        stages,
        5,
        "write-original-build-evidence",
        envelopes.envelope(
            "WRITING_ORIGINAL_BUILD_EVIDENCE",
            "SUCCEEDED",
            {"path": str(path), "schema": original_build_evidence.SCHEMA},
        ),
    )

def record_coverage_injection_evidence(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    number: int,
    name: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    return record(
        job,
        stages,
        number,
        name,
        envelopes.envelope("RECORDING_COVERAGE_INJECTION_EVIDENCE", "SUCCEEDED", state),
    )

def record(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    number: int,
    name: str,
    result: dict[str, Any],
    input_digest: str | None = None,
    reused_from: str | None = None,
) -> dict[str, Any]:
    now = time.monotonic()
    started, previous = _JOB_PROGRESS_CLOCKS.get(job["jobId"], (now, now))
    timing = result.setdefault("timing", {})
    timing["recordedAt"] = fileio.utc_now()
    timing.setdefault("startedAt", _JOB_PROGRESS_UTC.get(job["jobId"], timing["recordedAt"]))
    timing.setdefault("finishedAt", timing["recordedAt"])
    timing.setdefault("durationMs", round((now - previous) * 1000))
    timing.setdefault("measurement", "SINCE_PREVIOUS_RECORD")
    _JOB_PROGRESS_UTC[job["jobId"]] = timing["recordedAt"]
    timing["elapsedSincePreviousStageSeconds"] = round(now - previous, 3)
    timing["elapsedSinceJobStartSeconds"] = round(now - started, 3)
    _JOB_PROGRESS_CLOCKS[job["jobId"]] = (started, now)
    latest_path = stage_path(job, number, name)
    write_json(latest_path, result)
    event_path = (
        state_store.append_event(job, number, name, result, input_digest, reused_from)
        if job.get("jobDirectory")
        else latest_path
    )
    stages.append(
        {
            "number": number,
            "name": name,
            "status": result.get("status"),
            "path": str(latest_path),
            "eventPath": str(event_path),
            "inputDigest": input_digest,
            "reused": reused_from is not None,
        }
    )
    if job.get("jobDirectory"):
        write_json(
            Path(job["jobDirectory"]) / "workflow-state.json",
            {
                "schema": WORKFLOW_SCHEMA,
                "jobId": job["jobId"],
                "status": "RUNNING",
                "state": name,
                "stages": stages,
            },
        )
    print(
        f"[COVER {timing['elapsedSinceJobStartSeconds']:8.3f}s] "
        f"{number:02d} {name}: {result.get('status', 'UNKNOWN')}"
        f"{' [reused]' if reused_from else ''} "
        f"(+{timing['elapsedSincePreviousStageSeconds']:.3f}s)",
        file=sys.stderr,
        flush=True,
    )
    return result
