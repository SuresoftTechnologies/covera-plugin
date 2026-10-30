from __future__ import annotations

"""Durable, append-only stage evidence and input-fingerprint reuse."""

import hashlib
import json
import re
import copy
import time
from pathlib import Path
from typing import Any, Callable

from cover_build.core.errors import WorkflowWaiting
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import open_api


SCHEMA = "cover-workflow-stage-event/v1"
EXECUTOR_VERSION = "3"
_INVOCATIONS: dict[str, tuple[str, float]] = {}
_INPUT_WAITS: dict[str, int] = {}


def begin_invocation(job: dict[str, Any], started_at: str | None = None, started: float | None = None) -> None:
    _INVOCATIONS[job["jobId"]] = (started_at or fileio.utc_now(), time.monotonic() if started is None else started)
    _INPUT_WAITS[job["jobId"]] = 0


def record_input_wait(job: dict[str, Any], duration_ms: int) -> None:
    _INPUT_WAITS[job["jobId"]] = _INPUT_WAITS.get(job["jobId"], 0) + duration_ms


def end_invocation(job: dict[str, Any], status: str) -> None:
    start = _INVOCATIONS.pop(job["jobId"], None)
    if start is None:
        return
    path = Path(job["jobDirectory"]) / "execution-times.json"
    value = fileio.load_json(path) if path.is_file() else {"invocations": []}
    process_ms = round((time.monotonic() - start[1]) * 1000)
    wait_ms = _INPUT_WAITS.pop(job["jobId"], 0)
    value["invocations"].append({"startedAt": start[0], "finishedAt": fileio.utc_now(),
                                 "durationMs": max(0, process_ms - wait_ms), "inputWaitDurationMs": wait_ms, "status": status})
    fileio.atomic_write_json(path, value)


def performance_summary(job: dict[str, Any]) -> dict[str, Any]:
    path = Path(job["jobDirectory"]) / "execution-times.json"
    invocations = fileio.load_json(path).get("invocations", []) if path.is_file() else []
    active = sum(item["durationMs"] for item in invocations)
    waits: dict[str, int] = {"WAITING_MODEL": 0, "WAITING_USER": sum(i.get("inputWaitDurationMs", 0) for i in invocations), "OTHER": 0}
    for previous, current in zip(invocations, invocations[1:]):
        gap = max(0, round((open_api.parse_server_datetime(current["startedAt"]) -
                            open_api.parse_server_datetime(previous["finishedAt"])).total_seconds() * 1000))
        waits[previous["status"] if previous["status"] in waits else "OTHER"] += gap
    commands = []
    for record_path in (Path(job["jobDirectory"]) / "logs").glob("*.metrics.json"):
        commands.append(fileio.load_json(record_path))
    events = [fileio.load_json(p) for p in _events_directory(job).glob("*.json")]
    stages = [{"stage": e["logicalStageId"], "durationMs": e["result"].get("timing", {}).get("durationMs", 0)}
              for e in events if not e.get("reusedFrom")]
    return {"totalDurationMs": active + sum(waits.values()), "activeDurationMs": active,
            "externalWaitDurationMs": sum(waits.values()), "waitDurationsMs": waits,
            "resumeCount": max(0, len(invocations) - 1),
            "checkpointReuseCount": sum(bool(e.get("reusedFrom")) for e in events),
            "commandDurations": commands,
            "slowestStages": sorted(stages, key=lambda s: s["durationMs"], reverse=True)[:5]}


def input_digest(value: Any) -> str:
    payload = json.dumps(
        {"executorVersion": EXECUTOR_VERSION, "input": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "stage"


def _events_directory(job: dict[str, Any]) -> Path:
    path = Path(job["jobDirectory"]) / "events"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_event(
    job: dict[str, Any],
    number: int,
    name: str,
    result: dict[str, Any],
    digest: str | None = None,
    reused_from: str | None = None,
) -> Path:
    directory = _events_directory(job)
    sequence = max(
        [int(path.name.split("-", 1)[0]) for path in directory.glob("*.json") if path.name.split("-", 1)[0].isdigit()],
        default=0,
    ) + 1
    path = directory / f"{sequence:06d}-{number:02d}-{_safe_name(name)}.json"
    fileio.atomic_write_json(
        path,
        {
            "schema": SCHEMA,
            "sequence": sequence,
            "jobId": job["jobId"],
            "logicalStageId": name,
            "number": number,
            "inputDigest": digest,
            "executorVersion": EXECUTOR_VERSION,
            "recordedAt": fileio.utc_now(),
            "reusedFrom": reused_from,
            "result": result,
        },
    )
    return path


def find_succeeded(
    job: dict[str, Any],
    name: str,
    digest: str,
) -> tuple[dict[str, Any], Path] | None:
    directory = _events_directory(job)
    paths = sorted(directory.glob(f"*-*-{_safe_name(name)}.json"), reverse=True)
    for path in paths:
        try:
            event = fileio.load_json(path)
        except (OSError, ValueError):
            continue
        result = event.get("result")
        if (
            event.get("schema") == SCHEMA
            and event.get("jobId") == job.get("jobId")
            and event.get("logicalStageId") == name
            and event.get("inputDigest") == digest
            and event.get("executorVersion") == EXECUTOR_VERSION
            and isinstance(result, dict)
            and result.get("status") == "SUCCEEDED"
        ):
            return result, path
    return None


def run_checkpointed(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    number: int,
    name: str,
    inputs: Any,
    action: Callable[[], dict[str, Any]],
    validate: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    digest = input_digest(inputs)
    if not job.get("jobDirectory"):
        return record(job, stages, number, name, action(), digest, None)
    cached = find_succeeded(job, name, digest)
    if cached is not None and (validate is None or validate(cached[0])):
        result, path = cached
        result = copy.deepcopy(result)
        now = fileio.utc_now()
        result["timing"] = {"startedAt": now, "finishedAt": now, "durationMs": 0, "measurement": "REUSED"}
        return record(job, stages, number, name, result, digest, str(path))
    started_at, started = fileio.utc_now(), time.monotonic()
    try:
        result = action()
    except Exception as exc:
        status = f"WAITING_{exc.decision_owner}" if isinstance(exc, WorkflowWaiting) else "FAILED"
        result = envelopes.envelope("EXECUTING_STAGE", status, {"errorCode": getattr(exc, "code", type(exc).__name__)})
        result["timing"] = {"startedAt": started_at, "finishedAt": fileio.utc_now(),
                            "durationMs": round((time.monotonic() - started) * 1000), "measurement": "ACTION"}
        record(job, stages, number, name, result, digest, None)
        raise
    result["timing"] = {"startedAt": started_at, "finishedAt": fileio.utc_now(),
                        "durationMs": round((time.monotonic() - started) * 1000), "measurement": "ACTION"}
    return record(job, stages, number, name, result, digest, None)
