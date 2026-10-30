"""Read COVER build context and locate stored error artifacts without rebuilding."""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .agent import hardware_id_from_file
from .common import RecoveryError, decode_bytes

CONFIG_LIMIT = 1024 * 1024


def resolve_source(source_path: str, project_root: Path | None = None) -> str:
    if not isinstance(source_path, str) or not source_path.strip():
        raise RecoveryError("MissingSource", "오류 소스명과 빌드 작업 폴더가 필요합니다.")
    value = source_path.strip()
    if PureWindowsPath(value).is_absolute() and os.name != "nt":
        return str(PureWindowsPath(value))
    if value.startswith("/") and os.name == "nt":
        return value
    return os.path.abspath(Path(project_root or Path.cwd()) / value)


def _native_path(value: str) -> bool:
    return not (os.name == "nt" and value.startswith("/")) and not (
        os.name != "nt" and PureWindowsPath(value).is_absolute()
    )


def _read_config(path: Path) -> str:
    _check_local_path(path)
    if path.stat().st_size > CONFIG_LIMIT:
        raise RecoveryError("ConfigurationLimit", "설정 파일 크기 한도를 넘습니다.")
    return decode_bytes(path.read_bytes())[0]


def _check_local_path(path: Path) -> None:
    if str(path).startswith(("\\\\", "//")):
        raise RecoveryError("UnsafeConfiguration", "네트워크 경로는 로컬 탐색에서 열지 않습니다.")
    for parent in reversed((path, *path.parents)):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RecoveryError("UnsafeConfiguration", "링크 경로는 자동으로 따라가지 않습니다.")


def _local_absolute(value: str | Path, label: str, warnings: list[str]) -> Path | None:
    raw = str(value)
    if not _native_path(raw) or not Path(raw).is_absolute() or raw.startswith(("\\\\", "//")):
        warnings.append(f"로컬 절대 경로를 확인해야 함: {label}")
        return None
    # resolve()로 링크를 따라가기 전에 읽기 경계에서 검사한다.
    return Path(os.path.abspath(raw))


def _children(root: Path, warnings: list[str], limit: int = 80) -> list[Path]:
    try:
        _check_local_path(root)
        found = []
        with os.scandir(root) as entries:
            for entry in entries:
                if len(found) >= limit:
                    warnings.append(f"폴더 탐색 한도 초과: {root}")
                    break
                found.append(root / entry.name)
        return sorted(found, key=lambda path: path.name)
    except FileNotFoundError:
        return []
    except (OSError, RecoveryError):
        warnings.append(f"폴더를 안전하게 읽지 못함: {root}")
        return []


def _properties(path: Path, keys: set[str], warnings: list[str]) -> dict[str, str] | None:
    if not path.is_file():
        return {}
    try:
        values, pending = {}, ""
        escapes = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}

        def unescape(match):
            token = match.group(1)
            return chr(int(token[1:], 16)) if token.startswith("u") else escapes.get(token, token)

        for raw in _read_config(path).splitlines():
            line = pending + raw.lstrip() if pending else raw
            if (len(line) - len(line.rstrip("\\"))) % 2:
                pending = line[:-1]
                continue
            pending = ""
            if line.lstrip().startswith(("#", "!")):
                continue
            for key in keys:
                match = re.match(r"^\s*" + re.escape(key) + r"(?:\s*[=:]\s*|\s+)(.*)$", line)
                if match:
                    value = match.group(1).strip()
                    if re.search(r"\\u(?![0-9a-fA-F]{4})", value):
                        raise ValueError("invalid property escape")
                    values[key] = re.sub(r"\\(u[0-9a-fA-F]{4}|.)", unescape, value)
        if pending:
            raise ValueError("unfinished continuation")
        return values
    except (OSError, RecoveryError, UnicodeError, ValueError):
        warnings.append(f"설정 파일을 확인하지 못함: {path}")
        return None


def _agent_server_setting(path: Path, warnings: list[str]) -> tuple[bool | None, str | None]:
    if not path.is_file():
        return False, None
    try:
        fields = {}
        for line in _read_config(path).splitlines():
            match = re.match(r"^(usingFixedServerUrl|fixedServerUrl)\s*:\s*(.*?)\s*$", line)
            if match:
                key, value = match.groups()
                if key in fields:
                    raise ValueError("duplicate setting")
                fields[key] = value.split(" #", 1)[0].strip().strip("\"'")
        fixed = fields.get("usingFixedServerUrl", "false").lower()
        if fixed not in {"true", "false"}:
            raise ValueError("unknown fixed URL mode")
        return fixed == "true", fields.get("fixedServerUrl") or None
    except (OSError, RecoveryError, UnicodeError, ValueError):
        warnings.append(f"Agent 서버 설정을 확인하지 못함: {path}")
        return None, None


def _safe_url(value: str | None, origin: str, warnings: list[str]) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(ord(char) < 32 for char in value)):
            raise ValueError("unsafe URL")
        _ = parsed.port
        return value.strip().rstrip("/")
    except (ValueError, AttributeError):
        warnings.append(f"서버 주소 형식을 확인해야 함: {origin}")
        return None


def _local_server(url: str | None) -> bool:
    if not url:
        return False
    host = urlsplit(url).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _homes(root: Path, warnings: list[str]) -> list[Path]:
    choices = [root, root / "tomcat"]
    choices += [p for p in _children(root, warnings) if p.name.lower().startswith("apache-tomcat")]
    homes = []
    for path in choices:
        try:
            _check_local_path(path)
            if (path / "properties" / "issue-manager.properties").is_file() or (path / "properties" / "remote-service.properties").is_file():
                homes.append(path)
        except (OSError, RecoveryError):
            continue
    return homes


def _neighbor_homes(agent: Path, warnings: list[str]) -> list[Path]:
    found = []
    for base in (agent.parent, agent.parent.parent):
        if base == Path(base.anchor):
            continue
        for child in _children(base, warnings):
            if "server" not in child.name.lower():
                continue
            found.extend(_homes(child, warnings))
            for nested in _children(child, warnings):
                if "server" in nested.name.lower():
                    found.extend(_homes(nested, warnings))
    return list(dict.fromkeys(found))


def _server_ports(home: Path) -> set[int]:
    try:
        root = ET.fromstring(_read_config(home / "conf" / "server.xml"))
        return {int(item.attrib["port"]) for item in root.iter("Connector")
                if item.attrib.get("port", "").isdigit()}
    except (OSError, RecoveryError, ET.ParseError):
        return set()


def _absolute_setting(value: str, label: str, warnings: list[str]) -> Path | None:
    if not _native_path(value) or not Path(value).is_absolute() or "${" in value or "%" in value:
        warnings.append(f"서버 실행 작업 폴더 또는 변수 확인이 필요한 경로: {label}")
        return None
    return _local_absolute(value, label, warnings)


def discover_context(
    source_path: str, *, project_root: Path | None = None, agent_root: Path | None = None,
    server_root: Path | None = None, server_url: str | None = None,
    hardware_file: Path | None = None, search_roots: list[Path] | None = None,
    hardware_id: str | None = None,
    environ: dict[str, str] | None = None, user_home: Path | None = None,
    access_key_env: str = "COVER_ACCESS_KEY",
) -> dict:
    """Return allowlisted paths/URL and credential-presence booleans only."""
    env = os.environ if environ is None else environ
    account_home = Path(os.path.abspath(user_home or Path.home()))
    project = Path(os.path.abspath(project_root or Path.cwd()))
    source = resolve_source(source_path, project)
    warnings: list[str] = []
    roots, locations = [], []

    def add_root(path: Path, origin: str):
        path = _local_absolute(path, origin, warnings)
        if path is None:
            return
        if path == Path(path.anchor):
            warnings.append(f"전체 드라이브 탐색은 생략: {origin}")
            return
        key = str(path)
        try:
            _check_local_path(path)
            exists = path.exists()
        except FileNotFoundError:
            exists = False
        except (OSError, RecoveryError):
            warnings.append(f"탐색 경로를 안전하게 열지 못함: {path}")
            return
        if key not in roots:
            roots.append(key)
        locations.append({"path": key, "origin": origin, "exists": exists})

    add_root(project, "build_project")
    if _native_path(source) and Path(source).parent != project:
        add_root(Path(source).parent, "source_parent")
    agent_value = str(agent_root) if agent_root else env.get("CODESCROLL_TM_INSTALL_PATH")
    agent = _local_absolute(agent_value, "Agent 설치", warnings) if agent_value else None
    fixed, fixed_url = _agent_server_setting(agent / "bin" / "agent-config.yaml", warnings) if agent else (False, None)
    url_origin = "explicit" if server_url else None
    # Issue lookup/download uses the documented main-server endpoint. The
    # Agent fixed URL is only an analysis-server hint and must not override it.
    raw_url = server_url
    url = _safe_url(raw_url, url_origin, warnings)
    analysis_url = _safe_url(fixed_url, "agent-config.yaml:fixedServerUrl", warnings) if fixed else None
    if fixed and not fixed_url:
        warnings.append("Agent가 고정 분석 서버를 사용하지만 fixedServerUrl이 비어 있음.")
    if agent:
        for name in ("tmp", "logs"):
            add_root(agent / name, "agent_" + name)
        if env.get("COVER_WORKSPACE") or env.get("COVER_STANDALONE_INSTALL_PATH"):
            warnings.append("Agent 흐름에서는 별도 standalone 설치의 workspace를 자동으로 섞지 않음.")
    else:
        if env.get("COVER_WORKSPACE"):
            add_root(Path(env["COVER_WORKSPACE"]), "env_workspace_hint")
        standalone = env.get("COVER_STANDALONE_INSTALL_PATH") or env.get("COVER_INSTALL_PATH")
        if standalone:
            for name in ("works", "logs"):
                add_root(Path(standalone) / name, "standalone_layout_hint")
    choices = []
    if server_root:
        local_root = _local_absolute(Path(os.path.abspath(server_root)), "서버 설치", warnings)
        if local_root:
            choices += [(home, "explicit_server_root") for home in _homes(local_root, warnings)]
    # CODESCROLL_TM_SERVER_URL belongs to Agent -> remote-service traffic. It
    # is deliberately not used as a server API endpoint or local-server hint.
    local_urls = [value for value in (analysis_url,) if _local_server(value)]
    if local_urls:
        if env.get("CATALINA_HOME"):
            catalina = _local_absolute(env["CATALINA_HOME"], "env:CATALINA_HOME", warnings)
            if catalina:
                choices += [(home, "env:CATALINA_HOME") for home in _homes(catalina, warnings)]
        if agent:
            choices += [(home, "near_agent_installation") for home in _neighbor_homes(agent, warnings)]
    servers, seen = [], set()
    if len(choices) > 64:
        warnings.append("서버 설치 후보 한도 초과. 서버 루트를 좁혀야 함.")
    for home, origin in choices[:64]:
        if home in seen:
            continue
        seen.add(home)
        ports = _server_ports(home)
        expected_ports = {urlsplit(value).port or (443 if urlsplit(value).scheme == "https" else 80) for value in local_urls}
        if origin != "explicit_server_root" and not expected_ports.intersection(ports):
            continue
        remote = _properties(home / "properties" / "remote-service.properties",
                             {"Configuration.EXTENAL_PATH", "Configuration.WORK_PATH"}, warnings)
        issue = _properties(home / "properties" / "issue-manager.properties", {"Configuration.ISSUE_PATH"}, warnings)
        if remote is None or issue is None:
            continue
        external = _absolute_setting(remote["Configuration.EXTENAL_PATH"], "Configuration.EXTENAL_PATH", warnings) \
            if remote.get("Configuration.EXTENAL_PATH") else home.parent / "external"
        issue_path = _absolute_setting(issue["Configuration.ISSUE_PATH"], "Configuration.ISSUE_PATH", warnings) \
            if issue.get("Configuration.ISSUE_PATH") else external / "issue" if external else None
        work_base = _absolute_setting(remote["Configuration.WORK_PATH"], "Configuration.WORK_PATH", warnings) \
            if remote.get("Configuration.WORK_PATH") else external / "workspace" if external else None
        servers.append({
            "catalina_home": str(home), "origin": origin, "configured_ports": sorted(ports),
            "issue_path": str(issue_path) if issue_path else None, "running_server_verified": False,
        })
        if issue_path:
            add_root(issue_path, "server_issue_store:" + origin)
        if work_base:
            add_root(work_base / "workspace", "server_analysis_workspace:" + origin)
            add_root(work_base / "error", "legacy_server_error_hint:" + origin)
    for root in search_roots or []:
        add_root(Path(os.path.abspath(root)), "explicit_search_root")
    archives = [path for path in _children(account_home / "Downloads", warnings, 2000)
                if re.search(r"(?:cover|issue|errors?|오류).*\.zip$", path.name, re.I)]
    for path in archives[:20]:
        add_root(path, "download_name_hint_only")
    if len(archives) > 20:
        warnings.append("다운로드 ZIP 탐색 한도 초과. 이름은 소스 일치 근거가 아님.")
    identity_path = Path(hardware_file or account_home / ".cstm_HardwareAddress")
    if hardware_id is not None:
        if not isinstance(hardware_id, str) or not hardware_id.strip() or "\x00" in hardware_id:
            raise RecoveryError("InvalidInput", "비어 있지 않은 HardwareID가 필요합니다.")
        hardware = hardware_id
    else:
        try:
            _check_local_path(identity_path)
            hardware = hardware_id_from_file(identity_path)
        except (RecoveryError, OSError):
            hardware = None
            warnings.append("현재 빌드 계정의 HardwareID를 확인하지 못함.")
    return {
        "source_path": source, "project_root": str(project), "agent_root": str(agent) if agent else None,
        "server_url": url, "server_url_origin": url_origin if url else None,
        "analysis_server_url": analysis_url,
        "hardware_id": hardware, "hardware_file": str(identity_path),
        "http_transport_unsupported": bool(url and urlsplit(url).scheme == "http"),
        "credentials": {"access_key_env": access_key_env,
                        "access_key_present": bool(env.get(access_key_env, "").strip())},
        "server_installations": servers, "search_roots": roots, "locations": locations,
        "truncated": any("한도 초과" in warning for warning in warnings), "warnings": warnings,
    }


def discover_artifacts(source_path: str, **kwargs) -> dict:
    from .artifact_search import search_artifacts
    context = discover_context(source_path, **kwargs)
    scanned = search_artifacts(context["source_path"], [Path(path) for path in context["search_roots"]])
    candidates = scanned["candidates"]
    for item in candidates:
        path = Path(item["path"])
        locations = [entry for entry in context["locations"] if path == Path(entry["path"]) or path.is_relative_to(Path(entry["path"]))]
        item["origins"] = sorted({entry["origin"] for entry in locations})
        # Neither a matching source path nor an issue-looking ZIP filename
        # proves which build Agent produced this candidate.
        item["requires_server_confirmation"] = True
    truncated = bool(scanned.get("truncated") or context.get("truncated"))
    return {
        "status": "ServerLookupRequired", "source_path": context["source_path"], "context": context,
        "candidates": candidates, "selected_artifact": None, "selected_complete": False,
        "requested_hardware_id": context["hardware_id"],
        "evidence_files": scanned.get("evidence_files", []), "searched_roots": scanned.get("searched_roots", []),
        "truncated": truncated, "warnings": context["warnings"] + scanned.get("warnings", []),
        "next_action": "collect",
        "do_not_request": ["artifact_zip", "elog", "issue_id"],
        "note": "소스 전체 경로와 MAC 주소로 Open API에서 최신 PA 오류 ZIP을 받음. Issue ID와 발생 시각은 API에서 제공하지 않음.",
    }
