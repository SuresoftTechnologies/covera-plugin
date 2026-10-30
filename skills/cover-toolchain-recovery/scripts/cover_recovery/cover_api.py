"""COVER Open API v1을 환경변수의 Access key로 호출한다.

Access key는 파일이나 로그에 남기지 않는다. 서버 변경 범위 확인은 호출자 책임이며,
변경 결과를 확인할 수 없으면 자동 재전송 없이 AmbiguousWrite를 반환한다.
"""

from __future__ import annotations

import copy
import http.client
import io
import json
import math
import ntpath
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from typing import Any

from .common import RecoveryError


_COMPILER_KINDS = {"C_COMPILER", "CPP_COMPILER", "C_CPP_COMPILER"}
_TOOLCHAIN_FIELDS = (
    "aliasName", "majorKind", "minorKind", "majorVersion", "minorVersion",
    "targetKind", "toolChainContent",
)
_AGENT_TRANSIENT_FIELDS = {"online", "startDate"}


_AGENT_SET_FIELDS = {"toolChains", "exclusionPolicies", "inclusions"}


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RecoveryError("InvalidInput", f"{field}에 비어 있지 않은 문자열이 필요함.")
    return value


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (
        443 if parsed.scheme.lower() == "https" else 80
    )


class _RedirectRejected(Exception):
    def __init__(self, cross_origin: bool):
        self.cross_origin = cross_origin


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """읽기와 쓰기 모두 재전송하지 않으며 다른 origin으로 Access key를 보내지 않는다."""

    def __init__(self, origin: tuple[str, str, int]):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            cross_origin = _origin(newurl) != self.origin
        except (ValueError, TypeError):
            cross_origin = True
        fp.close()
        raise _RedirectRejected(cross_origin)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise ValueError("non-finite number")


def _mac_address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}", value):
        raise RecoveryError("InvalidInput", "HardwareID는 MAC 주소 6개 쌍이어야 함.")
    return value.replace(":", "-").upper()


def _agent_configuration(agent: Mapping[str, Any]) -> dict:
    result = copy.deepcopy(dict(agent))
    for key in _AGENT_TRANSIENT_FIELDS:
        result.pop(key, None)
    # 서버 DTO의 Set은 응답 때 순서가 달라질 수 있다. includePathFlag 같은 List는 유지.
    for key in _AGENT_SET_FIELDS:
        if isinstance(result.get(key), list):
            result[key] = sorted(result[key], key=lambda item: json.dumps(item, sort_keys=True))
    return result


class CoverOpenApiClient:
    """명시한 한 서버에만 연결하는 표준 라이브러리 클라이언트.

    server_url은 서버 루트 또는 /cover-open-api[/v1]로 끝나는 주소. HTTP는 allow_http=True를
    명시해야 한다. HTTPS 인증서와 hostname 검증은 기본으로 생략하며,
    insecure_tls=False를 명시하면 검증한다.
    """

    def __init__(
        self,
        server_url: str,
        *,
        timeout: float = 30,
        allow_http: bool = False,
        insecure_tls: bool = True,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_archive_bytes: int = 256 * 1024 * 1024,
    ):
        _required_string(server_url, "server_url")
        try:
            if server_url != server_url.strip() or any(ord(ch) < 32 for ch in server_url):
                raise ValueError("control character")
            parsed = urllib.parse.urlsplit(server_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment):
                raise ValueError("invalid URL")
            _origin(server_url)  # 잘못된 포트도 요청 전에 거절.
            decoded_path = urllib.parse.unquote(parsed.path)
            if ("\\" in decoded_path or any(ord(ch) < 32 for ch in decoded_path)
                    or any(part in {".", ".."} for part in decoded_path.split("/"))):
                raise ValueError("unsafe path")
        except (TypeError, ValueError):
            raise RecoveryError("InvalidServerUrl", "인증 정보·쿼리·fragment 없는 HTTP(S) 서버 주소가 필요함.") from None
        if not isinstance(allow_http, bool):
            raise RecoveryError("InvalidInput", "allow_http는 명시적인 boolean 값이어야 함.")
        if not isinstance(insecure_tls, bool):
            raise RecoveryError("InvalidInput", "insecure_tls는 명시적인 boolean 값이어야 함.")
        if parsed.scheme == "http" and not allow_http:
            raise RecoveryError("InsecureTransport", "HTTP 연결은 allow_http를 명시적으로 허용해야 함.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise RecoveryError("InvalidInput", "timeout은 양의 초 단위 숫자여야 함.")
        for limit in (max_response_bytes, max_archive_bytes):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise RecoveryError("InvalidInput", "응답 크기 제한은 양의 정수여야 함.")
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/cover-api"):
            raise RecoveryError("InvalidServerUrl", "서버 루트 또는 /cover-open-api 주소를 사용해야 함.")
        if base_path.endswith("/cover-open-api/v1"):
            base_path = base_path[:-3]
        if not base_path.endswith("/cover-open-api"):
            base_path += "/cover-open-api"
        base_path += "/v1"
        self.api_base_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_archive_bytes = max_archive_bytes
        self._access_key = None
        tls_context = ssl._create_unverified_context() if insecure_tls else ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=tls_context),
            _NoRedirect(_origin(self.api_base_url)),
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        """메모리에 보관한 Access key 참조를 해제한다."""
        self._access_key = None

    def _request(
        self, method: str, endpoint: str, *, query: dict | None = None,
        payload: Any = None, statuses: tuple[int, ...] = (200,),
        accept: str = "application/json", limit: int | None = None,
        mutation: bool = False,
    ) -> tuple[bytes, str, dict[str, str]]:
        if not self._access_key:
            raise RecoveryError("AccessKeyRequired", "COVER Access key 환경변수를 먼저 설정해야 함.")
        url = self.api_base_url + endpoint
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": accept, "Accept-Encoding": "identity", "User-Agent": "cover-toolchain-recovery/1"}
        headers["Authorization"] = "Bearer " + self._access_key
        body = None
        if payload is not None:
            try:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise RecoveryError("InvalidInput", "요청을 JSON으로 표현할 수 없음.") from None
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        details = {"method": method, "endpoint": endpoint, "retry_allowed": False}
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                if status not in statuses:
                    raise RecoveryError("InvalidApiResponse", "예상하지 않은 HTTP 성공 상태.", {**details, "status": status})
                max_bytes = self.max_response_bytes if limit is None else limit
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_length = int(declared)
                    except ValueError:
                        raise RecoveryError("InvalidApiResponse", "응답 크기 헤더가 잘못됨.", details) from None
                    if declared_length < 0:
                        raise RecoveryError("InvalidApiResponse", "응답 크기 헤더가 잘못됨.", details)
                    if declared_length > max_bytes:
                        raise RecoveryError("ApiResponseTooLarge", "응답이 허용 크기를 초과함.", details)
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise RecoveryError("ApiResponseTooLarge", "응답이 허용 크기를 초과함.", details)
                if declared is not None and len(data) != declared_length:
                    raise RecoveryError("InvalidApiResponse", "응답이 끝까지 수신되지 않음.", details)
                content_type = response.headers.get_content_type().lower()
                # Set-Cookie와 서버 응답 본문은 오류 상세에 포함하지 않는다.
                safe_headers = {"status": str(status)}
                if response.headers.get("failed-fetch-file-count") is not None:
                    safe_headers["failed-fetch-file-count"] = response.headers["failed-fetch-file-count"]
                if content_type == "text/html" or data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                    raise RecoveryError("HtmlApiResponse", "API 대신 HTML 페이지가 반환됨. Open API 주소를 확인해야 함.", details)
                return data, content_type, safe_headers
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 401:
                self.close()
                raise RecoveryError("AuthenticationFailed", "COVER Access key가 거절되었거나 만료됨.", {**details, "status": status}) from None
            if status == 403:
                raise RecoveryError("PermissionDenied", "이 COVER 작업을 수행할 권한이 없음.", {**details, "status": status}) from None
            if mutation and (status >= 500 or 300 <= status < 400):
                raise RecoveryError("AmbiguousWrite", "서버 변경 결과를 확인할 수 없음. 재전송하지 말고 서버 상태를 확인해야 함.", {**details, "status": status}) from None
            raise RecoveryError("ApiHttpError", "COVER API가 요청을 거절함.", {**details, "status": status}) from None
        except _RedirectRejected as error:
            code = "AmbiguousWrite" if mutation else "UnsafeRedirect" if error.cross_origin else "ApiRedirect"
            raise RecoveryError(code, "API 리다이렉트를 따르지 않음. 요청을 자동 재전송하지 않았음.", {**details, "cross_origin": error.cross_origin}) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            code = "AmbiguousWrite" if mutation else "ApiNetworkError"
            raise RecoveryError(code, "COVER 응답 수신을 완료하지 못함. 요청을 자동 재전송하지 않았음.", details) from None
        except RecoveryError as error:
            if mutation:
                raise RecoveryError("AmbiguousWrite", "서버 변경 응답을 확인할 수 없음. 재전송하지 말고 서버 상태를 확인해야 함.", {**details, "reason": error.code}) from None
            raise

    @staticmethod
    def _parse_json(data: bytes, content_type: str, expected_type: type) -> Any:
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise RecoveryError("InvalidApiResponse", "JSON 응답이 아닌 데이터가 반환됨.")
        try:
            result = json.loads(data.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_reject_nonfinite)
        except (UnicodeError, ValueError, RecursionError):
            raise RecoveryError("InvalidApiResponse", "JSON 응답을 안전하게 해석할 수 없음.") from None
        if not isinstance(result, expected_type):
            raise RecoveryError("InvalidApiResponse", "API 응답의 최상위 형식이 잘못됨.")
        return result

    def _json(self, method: str, endpoint: str, *, expected_type: type = dict, **kwargs) -> Any:
        data, content_type, _ = self._request(method, endpoint, **kwargs)
        try:
            return self._parse_json(data, content_type, expected_type)
        except RecoveryError as error:
            if kwargs.get("mutation"):
                raise RecoveryError("AmbiguousWrite", "서버 변경 응답이 유효한 JSON이 아님. 자동 재전송하지 않았음.", {"reason": error.code, "retry_allowed": False}) from None
            raise

    def access_key_from_env(self, access_key_env: str = "COVER_ACCESS_KEY") -> None:
        """키만 메모리에 읽는다. 로그인 요청이나 자동 재인증은 하지 않는다."""
        self.close()
        if not isinstance(access_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", access_key_env):
            raise RecoveryError("InvalidInput", "Access key 환경변수 이름이 잘못됨.")
        value = os.environ.get(access_key_env)
        if not value or not value.strip():
            raise RecoveryError("MissingAccessKey", "지정한 환경변수에 COVER Access key가 필요함.")
        # Header injection, non-ASCII and pasted 'Bearer ' prefixes fail before any request.
        if any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
            raise RecoveryError("InvalidAccessKey", "환경변수에는 공백 없는 Access key 값만 설정해야 함.")
        self._access_key = value

    def download_issue_archive(self, source_path: str, hardware_id: str) -> bytes:
        """서버가 MAC 주소와 전체 소스 경로로 선택한 최신 PA 오류 ZIP을 받는다."""
        _required_string(source_path, "source_path")
        if not (source_path.startswith("/") or ntpath.isabs(source_path) and ntpath.splitdrive(source_path)[0]):
            raise RecoveryError("InvalidInput", "최신 오류 선택에는 소스 전체 경로가 필요함.")
        data, content_type, headers = self._request(
            "GET", "/issue/download", query={"macAddress": _mac_address(hardware_id),
                "sourceFilePath": source_path, "withSource": "true", "issueWorkType": "PA_ERROR_RESULT"},
            statuses=(200, 206), accept="application/zip", limit=self.max_archive_bytes,
        )
        try:
            failed_count = int(headers.get("failed-fetch-file-count", "0"))
        except ValueError:
            raise RecoveryError("InvalidApiResponse", "다운로드 실패 개수 헤더가 잘못됨.") from None
        if headers["status"] == "206" or failed_count != 0:
            raise RecoveryError("PartialArchive", "오류 ZIP 일부가 누락됨. 불완전한 자료로 검증하지 않음.")
        if content_type != "application/zip":
            raise RecoveryError("InvalidArchive", "오류 자료 응답이 ZIP 형식이 아님.")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if not archive.infolist():
                    raise zipfile.BadZipFile("empty archive")
        except (zipfile.BadZipFile, OSError, ValueError):
            raise RecoveryError("InvalidArchive", "유효한 오류 ZIP 자료를 받지 못함.") from None
        return data  # 파일 생성과 안전한 압축 해제는 별도 자료 수집 계층의 책임.

    def list_agents(self) -> list[dict]:
        items = self._json("GET", "/agents", expected_type=list)
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("agentName"), str):
                raise RecoveryError("InvalidApiResponse", "Agent 목록의 식별 정보가 잘못됨.")
            self._response_mac(item.get("macAddress"))
        return items

    @staticmethod
    def _response_mac(value) -> str:
        try:
            return _mac_address(value)
        except RecoveryError:
            raise RecoveryError("InvalidApiResponse", "Agent 응답의 MAC 주소가 잘못됨.") from None

    @staticmethod
    def _validate_agent(agent: dict) -> None:
        if any(not isinstance(agent.get(key), str) or not agent[key] for key in ("id", "macAddress", "userName")):
            raise RecoveryError("InvalidApiResponse", "Agent 식별 정보가 잘못됨.")
        if any(not isinstance(agent.get(key), list) for key in ("toolChains", "exclusionPolicies", "inclusions")):
            raise RecoveryError("InvalidApiResponse", "Agent 설정에 필요한 목록이 누락됨.")
        if any(not isinstance(agent.get(key), bool) for key in ("licenseActivation", "online", "controllable")):
            raise RecoveryError("InvalidApiResponse", "Agent 설정에 필요한 상태가 누락됨.")
        for item in agent["toolChains"]:
            if not isinstance(item, dict):
                raise RecoveryError("InvalidApiResponse", "Agent Wrapper 설정 형식이 잘못됨.")
            if any(not isinstance(item.get(key), str) or not item[key]
                   for key in ("wrapperName", "toolchainName", "toolKind", "encodingType")):
                raise RecoveryError("InvalidApiResponse", "Wrapper 설정의 필수 문자열이 누락됨.")
            if any(not isinstance(item.get(key), bool) for key in ("buildTracking", "detectIncludeOption")):
                raise RecoveryError("InvalidApiResponse", "Wrapper 설정의 필수 옵션이 누락됨.")
            if (item.get("includePathFlag") is not None and
                    (not isinstance(item["includePathFlag"], list) or
                     any(not isinstance(flag, str) for flag in item["includePathFlag"]))):
                raise RecoveryError("InvalidApiResponse", "Wrapper include 플래그 형식이 잘못됨.")
            info = item.get("toolChainInfo")
            if item["toolKind"] in _COMPILER_KINDS and (
                    not isinstance(info, dict) or not isinstance(info.get("id"), str) or not info["id"]):
                raise RecoveryError("InvalidApiResponse", "컴파일러 Wrapper의 툴체인 정의 참조가 누락됨.")

    def get_agent(self, hardware_id: str) -> dict:
        mac = _mac_address(hardware_id)
        agent = self._json("GET", "/agent", query={"macAddress": mac})
        self._validate_agent(agent)
        if self._response_mac(agent["macAddress"]) != mac:
            raise RecoveryError("StaleTarget", "요청한 MAC 주소와 다른 Agent가 반환됨.")
        return agent

    def find_agent(self, hardware_id: str) -> dict:
        return self.get_agent(hardware_id)

    @staticmethod
    def _validate_toolchain(item: dict, *, require_id: bool, require_content: bool = False) -> None:
        string_fields = ("aliasName", "majorKind", "minorKind", "targetKind") + (("id",) if require_id else ())
        if any(not isinstance(item.get(key), str) or not item[key] for key in string_fields):
            raise RecoveryError("InvalidApiResponse", "툴체인 필수 문자열 정보가 누락됨.")
        if any(isinstance(item.get(key), bool) or not isinstance(item.get(key), int)
               for key in ("majorVersion", "minorVersion")):
            raise RecoveryError("InvalidApiResponse", "툴체인 버전 정보 형식이 잘못됨.")
        if not isinstance(item.get("original"), bool):
            raise RecoveryError("InvalidApiResponse", "툴체인 original 필드가 누락되었거나 형식이 잘못됨.")
        if (require_content or "toolChainContent" in item) and (
                not isinstance(item.get("toolChainContent"), list)
                or any(not isinstance(line, str) for line in item["toolChainContent"])):
            raise RecoveryError("InvalidApiResponse", "toolChainContent는 설정 줄의 배열이어야 함.")

    def list_toolchains(self) -> list[dict]:
        items = self._json("GET", "/toolchain-infos", expected_type=list)
        for item in items:
            if not isinstance(item, dict):
                raise RecoveryError("InvalidApiResponse", "툴체인 목록 형식이 잘못됨.")
            self._validate_toolchain(item, require_id=True)
        return items

    def get_toolchain(self, toolchain_id: str) -> dict:
        _required_string(toolchain_id, "toolchain_id")
        matches = [item for item in self.list_toolchains() if item["id"] == toolchain_id]
        if len(matches) != 1:
            raise RecoveryError("TargetNotFound" if not matches else "AmbiguousTarget", "요청한 툴체인 하나를 선택할 수 없음.")
        return matches[0]

    def create_toolchain(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise RecoveryError("InvalidInput", "툴체인 생성 입력은 객체여야 함.")
        self._validate_toolchain(payload, require_id=False, require_content=True)
        # 기존 id는 생성 요청에 보내지 않는다. 기존 툴체인을 수정하는 API는 제공하지 않음.
        proposed = {key: copy.deepcopy(payload[key]) for key in _TOOLCHAIN_FIELDS}
        if any(item["aliasName"] == proposed["aliasName"] for item in self.list_toolchains()):
            raise RecoveryError("ConflictingToolchain", "같은 이름의 툴체인이 이미 존재함. 자동 재사용하지 않음.")
        returned = self._json("POST", "/toolchain-infos", payload=proposed, statuses=(201,), mutation=True)
        try:
            self._validate_toolchain(returned, require_id=True)
            expected = {key: value for key, value in proposed.items() if key != "toolChainContent"}
            expected["original"] = False
            matches = [item for item in self.list_toolchains() if item["aliasName"] == proposed["aliasName"]]
            if (any(returned.get(key) != value for key, value in expected.items()) or len(matches) != 1
                    or matches[0]["id"] != returned["id"]
                    or any(matches[0].get(key) != value for key, value in expected.items())):
                raise RecoveryError("InvalidApiResponse", "생성된 툴체인의 표시 정보나 ID를 확인할 수 없음.")
            return matches[0]
        except RecoveryError as error:
            raise RecoveryError("AmbiguousWrite", "생성 요청 후 서버 재조회로 툴체인을 확인하지 못함. 자동 재전송하지 않았음.",
                                {"reason": error.code, "retry_allowed": False}) from None

    def update_agent_mapping(
        self, agent_id: str, hardware_id: str, wrapper_name: str, new_toolchain_id: str,
        expected_old_toolchain_id: str, expected_snapshot: dict | None = None,
    ) -> dict:
        for value, name in ((hardware_id, "hardware_id"), (wrapper_name, "wrapper_name"),
                            (new_toolchain_id, "new_toolchain_id"), (expected_old_toolchain_id, "expected_old_toolchain_id")):
            _required_string(value, name)
        current = self.get_agent(hardware_id)
        if current["id"] != agent_id:
            raise RecoveryError("StaleTarget", "계획 대상과 현재 Agent의 ID가 다름.")
        if expected_snapshot is not None:
            if not isinstance(expected_snapshot, dict) or _agent_configuration(current) != _agent_configuration(expected_snapshot):
                raise RecoveryError("StaleTarget", "계획 생성 이후 Agent 설정이 바뀜. 새 계획이 필요함.")
        same_wrapper = [item for item in current["toolChains"] if item.get("wrapperName") == wrapper_name]
        if len(same_wrapper) != 1:
            raise RecoveryError("TargetNotFound" if not same_wrapper else "AmbiguousTarget", "변경할 wrapper 하나를 선택할 수 없음.")
        if same_wrapper[0].get("toolKind") not in _COMPILER_KINDS:
            raise RecoveryError("UnsupportedMapping", "C/C++ 컴파일러 wrapper만 변경할 수 있음.")
        if same_wrapper[0]["toolChainInfo"]["id"] != expected_old_toolchain_id:
            raise RecoveryError("StaleTarget", "계획 대상의 기존 툴체인 연결이 바뀜.")
        replacement = self.get_toolchain(new_toolchain_id)
        proposed = copy.deepcopy(current)
        for item in proposed["toolChains"]:
            if item.get("wrapperName") == wrapper_name:
                item["toolChainInfo"] = {key: copy.deepcopy(replacement[key]) for key in
                                         ("id", "aliasName", "majorKind", "minorKind", "majorVersion", "minorVersion", "targetKind")}
        fields = ("toolchainName", "wrapperName", "toolKind", "testingStandardId", "buildTracking",
                  "encodingType", "detectIncludeOption", "includePathFlag")
        mappings = []
        for item in proposed["toolChains"]:
            mapping = {key: copy.deepcopy(item[key]) for key in fields if key in item}
            if item["toolKind"] in _COMPILER_KINDS:
                mapping["toolChainInfoId"] = item["toolChainInfo"]["id"]
            mappings.append(mapping)
        returned = self._json("PATCH", "/agent", query={"macAddress": _mac_address(hardware_id)},
                              payload={"toolChains": mappings}, mutation=True)
        try:
            self._validate_agent(returned)
            if _agent_configuration(returned) != _agent_configuration(proposed):
                raise RecoveryError("InvalidApiResponse", "변경 응답이 의도한 Agent 설정과 다름.")
            confirmed = self.get_agent(hardware_id)
            if _agent_configuration(confirmed) != _agent_configuration(proposed):
                raise RecoveryError("InvalidApiResponse", "다시 조회한 Agent 설정이 변경 내용과 다름.")
            return confirmed
        except RecoveryError as error:
            raise RecoveryError("AmbiguousWrite", "Agent 변경 후 저장된 설정을 확인하지 못함. 자동 재전송하지 않았음.",
                                {"reason": error.code, "retry_allowed": False}) from None
