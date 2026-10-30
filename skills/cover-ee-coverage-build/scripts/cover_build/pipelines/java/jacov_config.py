"""Create the missing user-home JACOV file without recording its key in job evidence."""

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from cover_build.core.errors import WorkflowWaiting
from cover_build.services import agent_open_api
from cover_build.common import agent_host
from cover_build.common import open_api
from cover_build.core import errors


def _read_agents(job: dict[str, Any], operation: str, query: list[str] | None = None) -> Any:
    args = agent_open_api._args(job, operation, query=query)
    args.sensitive_response = True
    # Do not pass this response through a stage recorder: Agent id is the Java key.
    return open_api.api_call(args)["data"]["response"]


def _write_new_config(path: Path, payload: bytes) -> None:
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
                raise OSError("Cannot write JACOV configuration")
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
        # No path-based cleanup: another process can change the file between
        # any ownership check and unlink. Keep even partial files untouched.


def create_home_config(job: dict[str, Any], path: Path, authenticate: Callable[[], None]) -> None:
    url = os.environ.get("CODESCROLL_TM_SERVER_URL", "").strip()
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(url)
        port = parsed.port
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        raise errors.ExecutorError("JACOV_CONFIG_INVALID", "CODESCROLL_TM_SERVER_URL must be a valid server URL")
    hardware_file = Path.home() / ".cstm_HardwareAddress"
    if not hardware_file.is_file():
        raise WorkflowWaiting("AGENT_IDENTITY_MISSING", "현재 계정의 .cstm_HardwareAddress가 필요합니다. Local Agent 식별 정보를 준비해 주세요.",
                              {}, decision_owner="USER", purpose="SERVER_API")
    hardware = agent_host.read_hardware_id_file(hardware_file)
    authenticate()
    agents = open_api.response_list(_read_agents(job, "getAgents"))
    matches = [item for item in agents if isinstance(item, dict)
               and str(item.get("macAddress", "")).casefold() == hardware.casefold()]
    if len(matches) != 1:
        raise WorkflowWaiting("JACOV_AGENT_NOT_UNIQUE", "현재 PC의 MAC과 일치하는 Agent가 정확히 하나여야 JACOV 설정을 생성할 수 있습니다.",
                              {}, decision_owner="USER", purpose="SERVER_API", evidence=[{"matchingCount": len(matches)}])
    key = matches[0].get("id")
    if key is None:
        detail = _read_agents(job, "getAgent", [f"macAddress={hardware}"])
        if not isinstance(detail, dict) or str(detail.get("macAddress", "")).casefold() != hardware.casefold():
            raise errors.ExecutorError("INVALID_OPEN_API_RESPONSE", "Agent detail does not match the current PC")
        key = detail.get("id")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", key):
        raise errors.ExecutorError("JACOV_AGENT_ID_INVALID", "Open API returned no valid Agent ID for JACOV")
    payload = (json.dumps({"key": key, "url": url}, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        _write_new_config(path, payload)
    except OSError:
        raise errors.ExecutorError(
            "JACOV_CONFIG_WRITE_FAILED",
            "JACOV 설정을 저장하지 못했습니다. 남은 파일을 자동 삭제하지 않았습니다. 파일과 저장 권한을 확인한 뒤 재개해 주세요.",
            [str(path)],
        ) from None
