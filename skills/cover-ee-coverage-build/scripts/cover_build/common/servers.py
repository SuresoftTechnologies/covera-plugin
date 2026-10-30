"""COVER server origin resolution for Open API and Local Agent connections."""

from __future__ import annotations

import argparse
import os
import socket
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import open_api_credentials
from cover_build.core import errors


SERVER_PATH_SUFFIXES = ("/cover-open-api",)


def server_origin(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise errors.ExecutorError("INVALID_SERVER_URL", f"Expected an absolute HTTP(S) COVER server URL: {value}")
    path = parsed.path.rstrip("/")
    for suffix in SERVER_PATH_SUFFIXES:
        if path.casefold().endswith(suffix.casefold()):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", "")).rstrip("/")


def open_api_server_origin(url_value: str) -> str:
    """Validate and normalize the user-supplied (or cached) Open API main web server URL."""
    origin = server_origin(url_value)
    if urllib.parse.urlsplit(origin).scheme.casefold() != "https":
        raise errors.ExecutorError(
            "INVALID_OPEN_API_MAIN_WEB_SERVER_URL",
            "The COVER Open API main web server URL must use HTTPS",
            [url_value],
        )
    return origin


def _tcp_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wsl_default_gateway() -> str | None:
    if not (os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")):
        return None
    route = Path("/proc/net/route")
    if not route.is_file():
        return None
    for line in route.read_text(encoding="ascii", errors="replace").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            gateway = socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
        except (ValueError, OSError):
            continue
        if flags & 0x2:
            return gateway
    return None


def adapt_wsl_loopback_origin(origin: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(origin)
    host = parsed.hostname
    if parsed.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"}:
        return origin, "NONE"
    port = parsed.port or 80
    if _tcp_reachable(host, port):
        return origin, "NONE"
    gateway = _wsl_default_gateway()
    if not gateway or not _tcp_reachable(gateway, port):
        return origin, "NONE"
    netloc = f"{gateway}:{port}" if parsed.port is not None else gateway
    adapted = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", "")).rstrip("/")
    return adapted, "WSL_HOST_GATEWAY"


def resolve_server_context(args: argparse.Namespace) -> dict[str, Any]:
    # Require the user's main web server URL before any remote-service probe.
    # CODESCROLL_TM_SERVER_URL only configures the Local Agent connection.
    open_api_url_value, open_api_url_source = open_api_credentials.resolve_open_api_server_url(args)
    origin = open_api_server_origin(open_api_url_value)
    candidates: list[tuple[str, str]] = []
    for source, value in (
        ("ARGUMENT:server-url", getattr(args, "server_url", None)),
        ("ARGUMENT:server-base-url", getattr(args, "server_base_url", None)),
    ):
        if value:
            candidates.append((source, value))
    if not candidates:
        for name in ("COVER_SERVER_URL", "CODESCROLL_TM_SERVER_URL"):
            value = os.environ.get(name)
            if value:
                candidates.append((f"ENVIRONMENT:{name}", value))
                break
    if not candidates:
        raise errors.ExecutorError(
            "SERVER_URL_REQUIRED",
            "Provide the Local Agent remote-service URL with --server-url or CODESCROLL_TM_SERVER_URL "
            "(legacy: COVER_SERVER_URL); provide the COVER main web server separately with --open-api-server-url",
        )
    resolved = [(source, server_origin(value)) for source, value in candidates]
    origins = {origin.casefold() for _, origin in resolved}
    if len(origins) != 1:
        raise errors.ExecutorError("SERVER_URL_MISMATCH", "Local Agent remote-service URL inputs resolve to different origins", resolved)
    configured_origin = resolved[0][1]
    network_origin, network_adaptation = adapt_wsl_loopback_origin(configured_origin)
    return {
        "configuredServerUrl": candidates[0][1],
        "agentServerOrigin": network_origin,
        "serverOrigin": origin,
        "configuredOpenApiServerUrl": open_api_url_value,
        "openApiServerUrlSource": open_api_url_source,
        "serverUrlSource": resolved[0][0],
        "serverNetworkAdaptation": network_adaptation,
        "serverTransportAdaptation": "HTTPS_MAIN_WEB_SERVER_URL",
        "serverBaseUrl": origin + "/cover-open-api",
    }


def job_agent_server_origin(job: dict[str, Any]) -> str:
    configured = job.get("agentServerOrigin")
    if isinstance(configured, str) and configured.strip():
        return server_origin(configured)
    original = job.get("configuredServerUrl")
    open_api_origin = job.get("serverOrigin")
    if isinstance(original, str) and isinstance(open_api_origin, str):
        original_scheme = urllib.parse.urlsplit(server_origin(original)).scheme.casefold()
        open_api_scheme = urllib.parse.urlsplit(server_origin(open_api_origin)).scheme.casefold()
        if original_scheme == "http" and open_api_scheme == "https":
            raise errors.ExecutorError(
                "AGENT_SERVER_ORIGIN_REQUIRED",
                "This protected job predates the Local Agent origin split; create a new job to preserve the configured HTTP scheme",
            )
    if not isinstance(open_api_origin, str) or not open_api_origin.strip():
        raise errors.ExecutorError("INVALID_JOB_CONTEXT", "The protected job does not contain a Local Agent server origin")
    return server_origin(open_api_origin)
