"""Typed result envelopes and the current job id shared by every stage."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Iterable

from cover_build.common import fileio
from cover_build.common import pathutil


VERSION = "1.14.0"


CURRENT_JOB_ID: str | None = None


def emit(value: Any, output: str | None = None) -> None:
    # Console encodings such as Windows CP949 cannot represent every value
    # captured from VsDevCmd.  ASCII-escaped JSON remains lossless and keeps
    # the command-line interface deterministic on every locale.
    text = json.dumps(value, ensure_ascii=True, indent=2) + "\n"
    if output:
        fileio.atomic_write_text(pathutil.canonical(output, False), text)
    else:
        sys.stdout.write(text)


def envelope(
    stage: str,
    status: str,
    data: Any = None,
    error: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    now = fileio.utc_now()
    return {
        "jobId": job_id or CURRENT_JOB_ID or str(uuid.uuid4()),
        "stage": stage,
        "status": status,
        "data": data or {},
        "evidence": [],
        "warnings": [],
        "error": error,
        "startedAt": now,
        "finishedAt": now,
        "executorVersion": VERSION,
    }
