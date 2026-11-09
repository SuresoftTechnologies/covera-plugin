"""Cache the COVER Open API main web server URL and Access Key under the Agent install root.

Unlike the Java key in ``cover_jacov.json`` (a product identifier, not a Bearer secret), the
Access Key cached here authenticates every Open API call. This is a narrow, explicit exception to
the general "never persist the Access Key" rule; see security-and-api.md.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any

from cover_build.core import errors


CREDENTIALS_RELATIVE_PATH = ("agent_kit", "open-api-credentials.json")


def credentials_path(install_path: str | os.PathLike[str]) -> Path:
    return Path(install_path).joinpath(*CREDENTIALS_RELATIVE_PATH)


def _installed_credentials_path() -> Path | None:
    install_path = os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    return credentials_path(install_path) if install_path else None


def load(path: Path) -> dict[str, str] | None:
    """Return the cached {openApiServerUrl, accessKey}, or None if the file does not exist."""
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.ExecutorError(
            "OPEN_API_CREDENTIALS_INVALID", "open-api-credentials.json cannot be read as JSON", [str(path)]
        ) from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("openApiServerUrl"), str)
        or not value["openApiServerUrl"].strip()
        or not isinstance(value.get("accessKey"), str)
        or not value["accessKey"].strip()
    ):
        raise errors.ExecutorError(
            "OPEN_API_CREDENTIALS_INVALID",
            "open-api-credentials.json must contain non-empty openApiServerUrl and accessKey fields",
            [str(path)],
        )
    return {"openApiServerUrl": value["openApiServerUrl"], "accessKey": value["accessKey"]}


def _write_new_file(path: Path, payload: bytes) -> None:
    """Create exclusively; on failure retain the file for user inspection."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        return
    written = 0
    failed = False
    try:
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError("Cannot write open-api-credentials.json")
            written += count
    except BaseException:
        failed = True
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            if not failed:
                raise


def save(path: Path, *, open_api_server_url: str, access_key: str) -> None:
    """Create the credentials file once; never overwrite an existing one."""
    payload = (
        json.dumps(
            {"openApiServerUrl": open_api_server_url, "accessKey": access_key},
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_new_file(path, payload)
    except OSError:
        raise errors.ExecutorError(
            "OPEN_API_CREDENTIALS_WRITE_FAILED",
            "Could not save open-api-credentials.json. The partial file was left untouched for inspection.",
            [str(path)],
        ) from None


def resolve_access_key(server_url: str | None = None) -> str | None:
    """Use an explicit process key, or a cached key bound to the selected API server."""
    existing = os.environ.get("COVER_ACCESS_KEY")
    if existing:
        return existing
    if not server_url:
        return None
    path = _installed_credentials_path()
    if path is None:
        return None
    record = load(path)
    if record is None:
        return None
    # Import locally: servers also uses this module to select the configured URL.
    from cover_build.common.servers import open_api_server_origin

    def identity(value: str) -> tuple:
        parsed = urllib.parse.urlsplit(open_api_server_origin(value))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("User information is not an API server identity")
        return parsed.scheme, parsed.hostname, parsed.port if parsed.port is not None else 443, parsed.path

    try:
        matches = identity(server_url) == identity(record["openApiServerUrl"])
    except (errors.ExecutorError, ValueError):
        return None
    return record["accessKey"] if matches else None


def authentication_server_url(args: argparse.Namespace) -> str | None:
    """Use the immutable job server on resume, otherwise select the new-job URL."""
    if getattr(args, "job_context", None):
        from cover_build.common.jobs import load_job_context
        return load_job_context(args.job_context)["serverOrigin"]
    try:
        return resolve_open_api_server_url(args)[0]
    except errors.ExecutorError as exc:
        if exc.code == "OPEN_API_MAIN_WEB_SERVER_URL_REQUIRED":
            return None  # No server means no cached key; the runner handles missing URL input.
        raise


def resolve_open_api_server_url(args: argparse.Namespace) -> tuple[str, str]:
    """Prefer a freshly provided URL, else the cached file. Raise if neither is available."""
    provided = getattr(args, "open_api_server_url", None)
    if provided is not None and str(provided).strip() != "":
        return str(provided), "USER_PROVIDED"
    path = _installed_credentials_path()
    if path is not None:
        record = load(path)
        if record is not None:
            return record["openApiServerUrl"], "PERSISTED_CONFIG"
    raise errors.ExecutorError(
        "OPEN_API_MAIN_WEB_SERVER_URL_REQUIRED",
        "Ask the user for the COVER Open API main web server URL and pass it with --open-api-server-url. "
        "CODESCROLL_TM_SERVER_URL is the Local Agent remote-service URL and cannot supply this value.",
    )


def remember(job: dict[str, Any]) -> None:
    """Best-effort cache of the confirmed Access Key alongside the resolved Open API URL.

    Caching is a convenience on top of an already-successful authentication; a write failure
    (disk permissions, race with another process) must not fail the coverage build itself.
    """
    path = _installed_credentials_path()
    if path is None:
        return
    access_key = os.environ.get(job.get("tokenEnvironmentName") or "COVER_ACCESS_KEY")
    open_api_server_url = job.get("serverOrigin")
    if not access_key or not isinstance(open_api_server_url, str) or not open_api_server_url.strip():
        return
    try:
        save(path, open_api_server_url=open_api_server_url, access_key=access_key)
    except errors.ExecutorError:
        pass
