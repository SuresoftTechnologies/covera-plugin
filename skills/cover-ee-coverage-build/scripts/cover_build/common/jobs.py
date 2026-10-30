"""Job context creation, loading, and integrity verification."""

from __future__ import annotations

import argparse
import os
import re
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import servers
from cover_build.core import errors


JOB_SCHEMA = "cover-build-job/v1"


def init_job(args: argparse.Namespace) -> dict[str, Any]:
    project_root = pathutil.canonical(args.project_root)
    server = servers.resolve_server_context(args)
    requested_tls_opt_out = getattr(args, "insecure_skip_tls_verify", None)
    if requested_tls_opt_out is not None and not isinstance(requested_tls_opt_out, bool):
        raise errors.ExecutorError("INVALID_TLS_OPTION", "insecure_skip_tls_verify must be boolean")
    server_is_https = urllib.parse.urlsplit(server["serverOrigin"]).scheme.casefold() == "https"
    insecure_skip_tls_verify = server_is_https if requested_tls_opt_out is None else requested_tls_opt_out
    if insecure_skip_tls_verify and urllib.parse.urlsplit(server["serverOrigin"]).scheme.casefold() != "https":
        raise errors.ExecutorError(
            "INSECURE_TLS_REQUIRES_HTTPS",
            "--insecure-skip-tls-verify is valid only for an HTTPS COVER server",
        )
    jobs_root = pathutil.canonical(args.jobs_root, False) if args.jobs_root else project_root / ".cover-build" / "jobs"
    token_env = args.token_env
    validate_environment_name(token_env)
    job_id = str(uuid.uuid4())
    job_directory = jobs_root / job_id
    job_directory.mkdir(parents=True, exist_ok=False)
    directories = {}
    for name in ("temporary", "logs", "stages"):
        path = job_directory / name
        path.mkdir()
        directories[name] = str(path)
    descriptor = fileio.add_integrity(
        {
            "schema": JOB_SCHEMA,
            "jobId": job_id,
            "projectRoot": str(project_root),
            "jobDirectory": str(job_directory),
            "directories": directories,
            **server,
            "tokenEnvironmentName": token_env,
            "insecureSkipTlsVerify": insecure_skip_tls_verify,
            "createdAt": fileio.utc_now(),
        }
    )
    context_path = job_directory / "job.json"
    fileio.atomic_write_json(context_path, descriptor)
    result = envelopes.envelope(
        "INITIALIZING_JOB",
        "SUCCEEDED",
        {**descriptor, "contextPath": str(context_path)},
        job_id=job_id,
    )
    fileio.atomic_write_json(job_directory / "stages" / "00-init-job.json", result)
    return result


def load_job_context(path: str | Path) -> dict[str, Any]:
    descriptor = fileio.load_json(path)
    if descriptor.get("schema") != JOB_SCHEMA:
        raise errors.ExecutorError("INVALID_JOB_CONTEXT", f"Expected {JOB_SCHEMA}")
    fileio.verify_integrity(descriptor)
    tls_opt_out = descriptor.get("insecureSkipTlsVerify", False)
    if not isinstance(tls_opt_out, bool):
        raise errors.ExecutorError("INVALID_JOB_CONTEXT", "insecureSkipTlsVerify must be boolean")
    open_api_origin = urllib.parse.urlsplit(str(descriptor.get("serverOrigin") or ""))
    if open_api_origin.scheme.casefold() != "https" or not open_api_origin.hostname or not open_api_origin.port:
        raise errors.ExecutorError(
            "INVALID_JOB_CONTEXT",
            "Create a new COVER job with a valid Open API main web server URL",
        )
    try:
        uuid.UUID(descriptor["jobId"])
    except (KeyError, ValueError, TypeError) as exc:
        raise errors.ExecutorError("INVALID_JOB_CONTEXT", "jobId is not a UUID") from exc
    job_directory = pathutil.canonical(descriptor.get("jobDirectory", ""))
    expected = {name: job_directory / name for name in ("temporary", "logs", "stages")}
    for name, path_value in expected.items():
        recorded = descriptor.get("directories", {}).get(name)
        if not recorded or not pathutil.same_path(recorded, path_value) or not path_value.is_dir():
            raise errors.ExecutorError("INVALID_JOB_CONTEXT", f"Missing or mismatched job directory: {name}")
    return descriptor


def auth_preflight(args: argparse.Namespace) -> dict[str, Any]:
    if not args.job_context:
        raise errors.ExecutorError("JOB_CONTEXT_REQUIRED", "auth-preflight requires --job-context from init-job")
    job = load_job_context(args.job_context)
    public_name = job["tokenEnvironmentName"]
    public_ready = bool(os.environ.get(public_name))
    in_memory_session = os.environ.get("COVER_CREDENTIAL_SESSION") == "1"
    missing: list[str] = []
    if args.require_public and not public_ready:
        missing.append(public_name)
    if missing:
        raise errors.ExecutorError(
            "AUTHENTICATION_INPUT_REQUIRED",
            "Required COVER authentication input is not available in the active credential session",
            missing,
        )
    return envelopes.envelope(
        "CHECKING_AUTHENTICATION_INPUTS",
        "SUCCEEDED",
        {
            "public": {
                "required": args.require_public,
                "ready": public_ready,
                "source": ("IN_MEMORY_CREDENTIAL_SESSION" if in_memory_session else f"ENVIRONMENT:{public_name}") if public_ready else None,
            },
            "credentialValuesPersisted": False,
        },
    )


def validate_environment_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise errors.ExecutorError("INVALID_TOKEN_ENV_NAME", f"Invalid environment variable name: {name}")


def argument(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)
