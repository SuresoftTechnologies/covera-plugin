from __future__ import annotations

"""Local Agent preparation backed only by COVER Open API v1."""

import os
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from cover_build.core.errors import WorkflowWaiting
from cover_build.services import agent_local as local
from cover_build.common import agent_host
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import open_api
from cover_build.common import pathutil
from cover_build.common import servers
from cover_build.core import errors


SKILL_ROOT = Path(__file__).parents[3]
OPEN_API_CONTRACT = SKILL_ROOT / "references" / "cover-open-api-v1.json"


def _args(
    job: dict[str, Any],
    operation: str,
    *,
    payload: str | None = None,
    query: list[str] | None = None,
    allow_mutation: bool = False,
) -> SimpleNamespace:
    tls_opt_out = job.get("insecureSkipTlsVerify", False)
    if not isinstance(tls_opt_out, bool):
        raise errors.ExecutorError("INVALID_JOB_CONTEXT", "insecureSkipTlsVerify must be boolean")
    return SimpleNamespace(
        job_context=str(Path(job["jobDirectory"]) / "job.json"),
        contract=str(OPEN_API_CONTRACT),
        base_url=job["serverBaseUrl"],
        operation=operation,
        payload=payload,
        query=query or [],
        allow_mutation=allow_mutation,
        skip_contract_check=False,
        token_env=job["tokenEnvironmentName"],
        ca_file=None,
        insecure_skip_tls_verify=tls_opt_out,
        timeout=30,
        output=None,
    )


def _call(job: dict[str, Any], operation: str, **kwargs: Any) -> dict[str, Any]:
    return open_api.api_call(_args(job, operation, **kwargs))


def _get_agent(job: dict[str, Any], hardware_id: str) -> dict[str, Any] | None:
    try:
        result = _call(job, "getAgent", query=[f"macAddress={hardware_id}"])
    except errors.ExecutorError as error:
        if error.code == "COVER_API_HTTP_ERROR" and "HTTP 404" in str(error):
            return None
        raise
    value = result["data"]["response"]
    return local.matching_agent(value, hardware_id)


def _record_agent(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    hardware_id: str,
    step: int,
    name: str,
) -> dict[str, Any] | None:
    result = _get_agent(job, hardware_id)
    if result is None:
        return None
    record(
        job,
        stages,
        step,
        name,
        envelopes.envelope("CALLING_COVER_API", "SUCCEEDED", {"operation": "getAgent", "response": result}),
    )
    return result


def _register_if_absent(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    descriptor: dict[str, Any],
    cover_user_name: str | None = None,
) -> bool:
    hardware_id = descriptor.get("hardwareId")
    if not isinstance(hardware_id, str) or not hardware_id:
        raise errors.ExecutorError("AGENT_IDENTITY_MISSING", "Local Agent registration requires a Java-generated hardware identity")
    if _get_agent(job, hardware_id):
        return False
    user_name = str(cover_user_name or os.environ.get("COVER_USER_NAME", "")).strip()
    monitor = str(Path(descriptor["agentRoot"]) / "coverage")
    payload = {
        "monitoringPath": monitor,
        "agentType": local.current_agent_type(),
        "agentName": local.new_agent_alias(job, descriptor, hardware_id),
        "licenseActivation": True,
        "macAddress": hardware_id,
        "permission": "Other",
    }
    if user_name:
        payload["userName"] = user_name
    payload_path = Path(job["directories"]["temporary"]) / "create-agent-open-api.json"
    fileio.atomic_write_json(payload_path, payload)
    try:
        created = record(job, stages, 21, "register-agent-open-api",
                         _call(job, "createAgent", payload=str(payload_path), allow_mutation=True))
    except errors.ExecutorError as error:
        if (not user_name and error.code == "COVER_API_HTTP_ERROR" and "HTTP 400" in str(error)
                and "built-in administrator account cannot own an agent" in str(error.evidence)):
            raise WorkflowWaiting(
                "AGENT_OWNER_REQUIRED", "현재 Access Key의 내장 관리자 계정은 Agent를 소유할 수 없습니다. 소유자 계정 이름을 알려주세요.",
                {"coverUserName": "USER_NAME"}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION",
            ) from error
        raise
    if not isinstance(created["data"].get("response"), dict):
        raise errors.ExecutorError("AGENT_REGISTRATION_FAILED", "Open API createAgent returned no Agent response")
    if not _record_agent(job, stages, record, hardware_id, 22, "verify-agent-registration-open-api"):
        raise errors.ExecutorError("AGENT_REGISTRATION_FAILED", "The created Agent was not returned by macAddress")
    return True


def _request_mapping(item: dict[str, Any]) -> dict[str, Any]:
    info = item.get("toolChainInfo")
    info_id = item.get("toolChainInfoId")
    if not info_id and isinstance(info, dict):
        info_id = info.get("id")
    required = {
        "toolchainName": item.get("toolchainName"),
        "wrapperName": item.get("wrapperName"),
        "toolKind": item.get("toolKind"),
    }
    if item.get("toolKind") in {"C_COMPILER", "CPP_COMPILER", "C_CPP_COMPILER"}:
        required["toolChainInfoId"] = info_id
    if not all(isinstance(value, str) and value for value in required.values()):
        raise errors.ExecutorError("INVALID_OPEN_API_RESPONSE", "Agent toolChains contains an incomplete mapping", [item])
    result = dict(required)
    for name in ("testingStandardId", "buildTracking", "encodingType", "detectIncludeOption", "includePathFlag"):
        if item.get(name) is not None:
            result[name] = item[name]
    return result


def _target_matches(requested: str, available: str) -> bool:
    requested = requested.casefold()
    available = available.casefold()
    if requested == available:
        return True
    if requested == "stm32":
        return False
    if requested == "arm32" and available in {"arm_general", "armv4i", "stm32"}:
        return True
    return available == "general"


def _select_info(wrapper: str, payload: dict[str, Any], available: list[Any]) -> dict[str, Any] | None:
    major_kind = payload.get("majorKind")
    candidates = [
        item
        for item in available
        if isinstance(item, dict)
        and item.get("majorKind") == major_kind
        and item.get("majorVersion") == payload.get("majorVersion")
        and _target_matches(str(payload.get("targetKind") or "general"), str(item.get("targetKind") or "general"))
    ]
    if major_kind == "cl":
        exact = [item for item in candidates if item.get("minorVersion") == payload.get("minorVersion")]
        candidates = exact or candidates
    else:
        alias = str(payload.get("aliasName") or "").casefold()
        if "mingw" in alias:
            matching = [item for item in candidates if item.get("minorKind") == "mingw" or "mingw" in str(item.get("aliasName") or "").casefold()]
            candidates = matching or candidates
        if "stm32" in alias:
            matching = [item for item in candidates if str(item.get("targetKind") or "").casefold() == "stm32"]
            candidates = matching or candidates
    return max(candidates, key=lambda item: (bool(item.get("original")), str(item.get("id") or ""))) if candidates else None


def _merge(current: Any, replacements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(current, list):
        raise errors.ExecutorError("INVALID_OPEN_API_RESPONSE", "Agent response has no toolChains array")
    replacement_by_name = {item["wrapperName"].casefold(): item for item in replacements}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in current:
        normalized = _request_mapping(item)
        key = normalized["wrapperName"].casefold()
        merged.append(replacement_by_name.get(key, normalized))
        seen.add(key)
    merged.extend(item for key, item in replacement_by_name.items() if key not in seen)
    return merged


def _model_toolchain_infos(job, profiles, available, selection):
    """Expose the complete API list; validate a job-bound model decision, never rank IDs."""
    evidence = {"jobId": job["jobId"], "observedCompilers": profiles, "toolChainInfos": available}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path = Path(job["directories"]["temporary"]) / "toolchain-info-candidates.json"
    fileio.atomic_write_json(path, {**evidence, "evidenceDigest": digest})

    def wait(message):
        raise WorkflowWaiting(
            "TOOLCHAIN_INFO_SELECTION_REQUIRED", message,
            {"toolChainInfoSelection": {}}, decision_owner="MODEL",
            purpose="TOOLCHAIN_INFO", evidence=[str(path)],
        )

    if not isinstance(selection, dict) or selection.get("evidenceDigest") != digest:
        wait("Select ToolChainInfo IDs for all observed compilers from the current API list; compare OS/ABI, architecture, family and version. Escalate if compatibility cannot be established.")
    mappings = selection.get("mappings")
    if not isinstance(mappings, dict) or set(mappings) != set(profiles):
        wait("Provide exactly one ID and nonempty compatibility reason per observed compiler wrapper.")
    selected = {}
    for wrapper, profile in profiles.items():
        choice = mappings[wrapper]
        if not isinstance(choice, dict) or not isinstance(choice.get("reason"), str) or not choice["reason"].strip():
            wait("Each compiler selection needs a nonempty compatibility reason.")
        matches = [item for item in available if isinstance(item, dict) and isinstance(choice.get("id"), str) and str(item.get("id")) == choice["id"]]
        if len(matches) != 1:
            wait("Selected ID must uniquely exist in the current API response.")
        info = matches[0]
        requested_family = local._gcc_family(str(profile.get("aliasName", "")))
        available_family = local._gcc_family(str(info.get("aliasName", "")) + " " + str(info.get("minorKind", "")))
        alias_architecture = local._architecture_from_text(str(info.get("aliasName", "")))
        conflict = (
            info.get("majorKind") != profile.get("majorKind")
            or info.get("majorVersion") != profile.get("majorVersion")
            or not _target_matches(str(profile.get("targetKind", "")), str(info.get("targetKind", "")))
            or (requested_family and available_family and requested_family != available_family)
            or (alias_architecture and alias_architecture != profile.get("targetKind"))
            or (("clang" in str(profile.get("aliasName", "")).lower()) != ("clang" in (str(info.get("aliasName", "")) + str(info.get("minorKind", ""))).lower()))
        )
        if conflict:
            wait(f"Incompatible ToolChainInfo selected for {wrapper}; do not substitute a different OS/ABI, architecture, compiler family or major version.")
        selected[wrapper] = info
    return selected


def _server_mappings_match(agent: dict[str, Any], expected: dict[str, Path], required_wrappers: set[str] | None, required_target: str | None = None, mapping_kinds: dict[str, str] | None = None, environment: dict[str, str] | None = None) -> bool:
    mappings = agent.get("toolChains")
    if not isinstance(mappings, list):
        return False
    expected_by_name = {name.casefold(): pathutil.canonical(path, False) for name, path in expected.items()}
    required = {name.casefold() for name in (required_wrappers or expected)}
    observed: dict[str, list[dict[str, Any]]] = {}
    for item in mappings:
        if isinstance(item, dict) and isinstance(item.get("wrapperName"), str):
            observed.setdefault(item["wrapperName"].casefold(), []).append(item)
    for name in required:
        candidates = observed.get(name, [])
        wanted = expected_by_name.get(name)
        if len(candidates) != 1 or wanted is None:
            return False
        item = candidates[0]
        raw = item.get("toolchainName")
        if not isinstance(raw, str):
            return False
        tool = raw.strip().strip('"')
        if not Path(tool).is_absolute() and "/" not in tool and "\\" not in tool and environment is not None:
            search_path = next((v for k, v in environment.items() if k.casefold() == "path"), "")
            tool = shutil.which(tool, path=search_path) or tool
        if not pathutil.same_path(tool, wanted):
            return False
        if item.get("toolKind") != (mapping_kinds or {}).get(name, local.wrapper_kind(name)):
            return False
        if item.get("toolKind") in {"C_COMPILER", "CPP_COMPILER", "C_CPP_COMPILER"}:
            info = item.get("toolChainInfo")
            if not isinstance(info, dict) or not info.get("id"):
                return False
            if required_target and str(info.get("targetKind", "")).casefold() != required_target.casefold():
                return False
    return True


def _reuse_original_tool_mappings(
    agent: dict[str, Any], expected: dict[str, Path], environment: dict[str, str],
    *, allow_wrapper_aliases: bool = True,
) -> tuple[dict[str, Path], dict[str, str]]:
    """Reuse a registered wrapper by original tool and role, never by basename alone.

    Bare server tool names are resolved in the verified original build environment.
    Resolution is for comparison only; existing server values remain unchanged.
    """
    current = agent.get("toolChains", [])
    search_path = next((value for key, value in environment.items() if key.casefold() == "path"), "")
    result: dict[str, Path] = {}
    kinds: dict[str, str] = {}
    occupied = {str(item.get("wrapperName", "")).casefold(): item for item in current}
    for default_name, real_tool in expected.items():
        if not real_tool.is_absolute() or not real_tool.is_file():
            raise errors.ExecutorError(
                "ORIGINAL_TOOL_PATH_UNRESOLVED",
                "Agent mappings require an existing absolute original tool path",
                [str(real_tool)],
            )
        kind = local.wrapper_kind(default_name)
        candidates = []
        for item in current:
            if not allow_wrapper_aliases and str(item.get("wrapperName", "")).casefold() != default_name.casefold():
                continue
            compatible_kinds = {kind, "C_CPP_COMPILER"} if kind in {"C_COMPILER", "CPP_COMPILER"} else {kind}
            if item.get("toolKind") not in compatible_kinds:
                continue
            raw = item.get("toolchainName")
            if not isinstance(raw, str) or not raw.strip():
                continue
            value = raw.strip().strip('"')
            path = Path(value)
            if not path.is_absolute():
                if "/" in value or "\\" in value:
                    continue  # No evidence of the Agent's relative working directory.
                found = shutil.which(value, path=search_path)
                if not found:
                    continue
                path = Path(found)
            if pathutil.same_path(path, real_tool):
                candidates.append(item)
        if len(candidates) > 1:
            raise WorkflowWaiting(
                "AGENT_WRAPPER_MAPPING_AMBIGUOUS",
                "Multiple registered wrappers match the original tool and role; select the intended mapping in COVER",
                {}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION",
                evidence=[{"originalTool": str(real_tool), "toolKind": kind,
                           "candidates": [_request_mapping(item) for item in candidates]}],
            )
        if candidates:
            name = candidates[0]["wrapperName"]
            kind = candidates[0]["toolKind"]
        else:
            name = f"cs{default_name}" if allow_wrapper_aliases else default_name
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise errors.ExecutorError("INVALID_OPEN_API_RESPONSE", "Invalid wrapper filename")
        if not candidates and name.casefold() in occupied:
            raise errors.ExecutorError("ORIGINAL_TOOLCHAIN_WRAPPER_NAME_COLLISION", "The default wrapper name is already assigned to another tool or role", [name, str(real_tool)])
        if name in result and (not pathutil.same_path(result[name], real_tool) or kinds[name.casefold()] != kind):
            raise errors.ExecutorError("ORIGINAL_TOOLCHAIN_WRAPPER_NAME_COLLISION", "Selected wrapper mappings conflict", [name])
        result[name] = real_tool
        kinds[name.casefold()] = kind
    return result, kinds


def _create_stm32_info(
    job: dict[str, Any], stages: list[dict[str, Any]], record: Callable[..., dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Create only the user-specified STM32 definition; never modify shared definitions."""
    if profile.get("majorKind") != "gcc" or profile.get("targetKind") != "STM32":
        raise errors.ExecutorError("INVALID_API_PAYLOAD", "Automatic definition creation is restricted to STM32 GCC")
    template = SKILL_ROOT / "assets" / "parser-config" / "stm32cubeide.conf"
    raw = template.read_bytes()
    payload = {**profile, "toolChainContent": raw.decode("utf-8").splitlines()}
    open_api.validate_payload(open_api.load_contract(OPEN_API_CONTRACT)["operations"]["createToolChainInfo"], payload)
    suffix = hashlib.sha256(fileio.canonical_json(profile)).hexdigest()[:12]
    directory = Path(job["directories"]["temporary"])
    marker = directory / f"toolchain-create-{suffix}.json"
    attempted = marker.exists()
    previous = fileio.load_json(marker) if attempted else {}
    if attempted and (not isinstance(previous, dict) or previous.get("state") != "REJECTED"):
        raise WorkflowWaiting(
            "TOOLCHAIN_CREATION_REVIEW_REQUIRED", "이 작업에서 정의 생성을 이미 시도했습니다. 서버 처리 결과를 확인한 뒤 재개해 주세요.",
            {}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION", evidence=[str(marker)],
        )
    request = directory / f"toolchain-create-{suffix}-request.json"
    fileio.atomic_write_json(request, payload)
    fileio.atomic_write_json(marker, {"state": "ATTEMPTED", "aliasName": payload["aliasName"],
                                      "parserSha256": hashlib.sha256(raw).hexdigest()})
    try:
        result = _call(job, "createToolChainInfo", payload=str(request), allow_mutation=True)
    except errors.ExecutorError as error:
        # Only explicit validation/auth rejections prove that no definition was
        # created. Conflict, transport errors and server errors remain uncertain.
        rejected = re.fullmatch(r"createToolChainInfo returned HTTP (400|401|403)", str(error))
        if error.code == "COVER_API_HTTP_ERROR" and rejected:
            fileio.atomic_write_json(marker, {"state": "REJECTED", "aliasName": payload["aliasName"],
                                                 "httpStatus": int(rejected.group(1)),
                                                 "parserSha256": hashlib.sha256(raw).hexdigest()})
        raise
    result["data"]["parserTemplate"] = {"path": str(template), "sha256": hashlib.sha256(raw).hexdigest()}
    record(job, stages, 23, f"create-stm32-toolchain-{suffix}", result)
    created = result["data"].get("response")
    if not isinstance(created, dict) or not created.get("id"):
        raise errors.ExecutorError("INVALID_OPEN_API_RESPONSE", "ToolChainInfo creation returned no ID; do not repeat the POST")
    fileio.atomic_write_json(marker, {"state": "CREATED", "id": created["id"], "aliasName": payload["aliasName"],
                                       "parserSha256": hashlib.sha256(raw).hexdigest()})
    listed = record(job, stages, 23, f"verify-stm32-toolchain-{suffix}", _call(job, "getToolChainInfos"))
    verified = next((item for item in open_api.response_list(listed["data"]["response"])
                     if isinstance(item, dict) and item.get("id") == created["id"]), None)
    if not verified or any(verified.get(key) != value for key, value in profile.items()):
        raise errors.ExecutorError("INVALID_OPEN_API_RESPONSE", "Created STM32 ToolChainInfo classification was not verified")
    return verified


def _update_agent_toolchain_mappings(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    descriptor: dict[str, Any],
    required_wrappers: set[str] | None,
    real_tools_override: dict[str, Path] | None,
    capability: str = "NATIVE",
    toolchain_selection: dict[str, Any] | None = None,
    mapping_kinds: dict[str, str] | None = None,
    original_environment: dict[str, str] | None = None,
) -> None:
    real_tools = local.required_real_tools(descriptor, required_wrappers, real_tools_override)
    def kind_of(name):
        return (mapping_kinds or {}).get(name.casefold(), local.wrapper_kind(name))
    agent = _record_agent(job, stages, record, descriptor["hardwareId"], 25, "get-agent-before-toolchains-open-api")
    if not agent:
        raise errors.ExecutorError("AGENT_REGISTRATION_FAILED", "No Agent matches the local macAddress")
    # Agent PATCH replaces the whole array. Only add absent names; never rewrite
    # a customer's existing definition, native path spelling or measurement options.
    missing = {}
    for wrapper, tool in real_tools.items():
        entries = [item for item in agent.get("toolChains", [])
                   if str(item.get("wrapperName", "")).casefold() == wrapper.casefold()]
        if not entries:
            missing[wrapper] = tool
        elif not _server_mappings_match(agent, {wrapper: tool}, {wrapper}, mapping_kinds=mapping_kinds,
                                        environment=original_environment):
            raise errors.ExecutorError("ORIGINAL_TOOLCHAIN_WRAPPER_NAME_COLLISION",
                                       "Existing wrapper mapping cannot be reused; it will not be overwritten", [wrapper])
    if not missing:
        return
    real_tools = missing
    compilers = {name: path for name, path in real_tools.items() if kind_of(name) in {"C_COMPILER", "CPP_COMPILER", "C_CPP_COMPILER"}}
    available = []
    if compilers:
        listed = record(job, stages, 23, "list-toolchain-infos-open-api", _call(job, "getToolChainInfos"))
        available = open_api.response_list(listed["data"]["response"])
    selected: dict[str, dict[str, Any]] = {}
    if compilers and capability != "STM32CUBEIDE":
        profiles = {wrapper: local.toolchain_profile(wrapper, tool, include_evidence=True) for wrapper, tool in compilers.items()}
        selected = _model_toolchain_infos(job, profiles, available, toolchain_selection)
    for wrapper, real_tool in compilers.items():
        if wrapper in selected:
            continue
        stm32 = capability == "STM32CUBEIDE" and "arm-none-eabi" in f"{wrapper} {real_tool}".casefold()
        profile = local.toolchain_profile(wrapper, real_tool, stm32)
        match = _select_info(wrapper, profile, available)
        if not match and stm32 and profile.get("targetKind") == "STM32":
            match = _create_stm32_info(job, stages, record, profile)
            available.append(match)
        if not match:
            raise WorkflowWaiting(
                "AGENT_TOOLCHAIN_UNPROVISIONED",
                "호환 ToolChainInfo가 없습니다. 확인된 파서 설정으로 Open API createToolChainInfo 또는 COVER UI에서 정의를 생성한 뒤 재개해 주세요.",
                {}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION",
                evidence=[{"wrapperName": wrapper, "observedTool": str(real_tool), "requiredProfile": profile}],
            )
        selected[wrapper] = match
    standards = record(job, stages, 24, "list-testing-standards-open-api", _call(job, "getTestingStandards"))
    standard_items = open_api.response_list(standards["data"]["response"])
    standard_id = next((str(item.get("id")) for item in standard_items if isinstance(item, dict) and item.get("id")), None)
    if not standard_id:
        raise errors.ExecutorError("UNKNOWN_TESTING_STANDARD", "The Open API returned no testing-standard ID")
    replacements = []
    for wrapper, real_tool in real_tools.items():
        kind = kind_of(wrapper)
        if kind is None:
            raise errors.ExecutorError("AGENT_TOOLCHAIN_UNPROVISIONED", f"Unsupported wrapper mapping: {wrapper}")
        replacement = {
            "toolchainName": str(real_tool),
            "wrapperName": wrapper,
            "toolKind": kind,
            "testingStandardId": standard_id,
            "buildTracking": False,
            "encodingType": "auto",
        }
        if kind in {"C_COMPILER", "CPP_COMPILER", "C_CPP_COMPILER"}:
            replacement["toolChainInfoId"] = str(selected[wrapper]["id"])
        replacements.append(replacement)
    latest = _record_agent(job, stages, record, descriptor["hardwareId"], 25, "get-agent-before-toolchains-patch-open-api")
    if not latest or latest.get("toolChains") != agent.get("toolChains"):
        raise WorkflowWaiting("AGENT_WRAPPER_MAPPING_AMBIGUOUS", "Agent mappings changed during preparation; retry with the current configuration",
                              {}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION")
    def selected_ids_match(value):
        infos = {item.get("wrapperName"): (item.get("toolChainInfo") or {}).get("id") for item in value.get("toolChains", [])}
        return all(str(infos.get(wrapper)) == str(info["id"]) for wrapper, info in selected.items())
    requested = _merge(agent.get("toolChains"), replacements)
    payload_path = Path(job["directories"]["temporary"]) / "update-agent-toolchains-open-api.json"
    fileio.atomic_write_json(payload_path, {"toolChains": requested})
    record(job, stages, 27, "update-agent-toolchains-open-api", _call(job, "updateAgent", payload=str(payload_path), query=[f"macAddress={descriptor['hardwareId']}"], allow_mutation=True))
    verified = _record_agent(job, stages, record, descriptor["hardwareId"], 28, "verify-agent-toolchains-open-api")
    target = "STM32" if capability == "STM32CUBEIDE" else None
    if not verified or not _server_mappings_match(verified, real_tools, set(real_tools), target, mapping_kinds) or not selected_ids_match(verified):
        raise errors.ExecutorError("AGENT_SERVER_TOOLCHAIN_UNPROVISIONED", "Open API Agent mapping verification failed")
    verified_entries = {item["wrapperName"].casefold(): _request_mapping(item) for item in verified.get("toolChains", [])}
    if any(verified_entries.get(item["wrapperName"].casefold()) != _request_mapping(item) for item in agent.get("toolChains", [])):
        raise errors.ExecutorError("AGENT_SERVER_TOOLCHAIN_UNPROVISIONED", "Existing Agent mappings changed during addition")


def _verify_runtime(
    job: dict[str, Any],
    descriptor: dict[str, Any],
    expected_tools: dict[str, Path],
    required_wrappers: set[str] | None,
    capability: str,
    timeout: int = 60,
) -> dict[str, Any]:
    listener_port = local.build_listener_port(capability)
    deadline = time.monotonic() + timeout
    while listener_port not in agent_host.ports_in_use([listener_port]):
        if time.monotonic() >= deadline:
            raise errors.ExecutorError("AGENT_BUILD_LISTENER_TIMEOUT", f"The Local Agent {capability} build listener did not become reachable", [listener_port])
        time.sleep(1)
    while True:
        server_agent = _get_agent(job, descriptor["hardwareId"])
        if server_agent and server_agent.get("online") is True:
            break
        if time.monotonic() >= deadline:
            raise errors.ExecutorError("AGENT_ONLINE_TIMEOUT", "The Open API did not report the local Agent online")
        time.sleep(1)
    monitor_text = server_agent.get("monitoringPath") or server_agent.get("logPath")
    if not isinstance(monitor_text, str) or not monitor_text.strip():
        raise errors.ExecutorError("AGENT_MONITOR_PATH_MISSING", "The Open API Agent has no monitoringPath")
    monitor = pathutil.canonical(monitor_text, False)
    while True:
        state = local.agent_runtime_log_state(Path(descriptor["agentRoot"]), monitor)
        if monitor.is_dir() and state["reportingInitialized"] and state["monitoringReady"]:
            break
        if time.monotonic() >= deadline:
            raise errors.ExecutorError("AGENT_RUNTIME_NOT_READY", "Local Agent reporting or monitoring path is not ready", [str(monitor)])
        time.sleep(1)
    return envelopes.envelope("VERIFYING_AGENT_RUNTIME", "SUCCEEDED", {"hardwareId": descriptor["hardwareId"], "serverOnline": True, "monitoringPath": str(monitor), "wrapperMappingsSynchronized": not expected_tools or local.local_mappings_match(descriptor, expected_tools, required_wrappers)})


def ensure_wrapper_mappings(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    allow_start: bool,
    required_wrappers: set[str] | None = None,
    real_tools_override: dict[str, Path] | None = None,
    capability: str = "NATIVE",
    cover_user_name: str | None = None,
    toolchain_selection: dict[str, Any] | None = None,
    original_environment: dict[str, str] | None = None,
    allow_wrapper_aliases: bool = True,
) -> dict[str, Any]:
    job_context = str(Path(job["jobDirectory"]) / "job.json") if job.get("jobDirectory") else None
    agent_server_origin = servers.job_agent_server_origin(job)
    discovered = record(job, stages, 6, "discover-agent", agent_host.discover_agent(SimpleNamespace(agent_root=None, server_url=agent_server_origin, service_name=None, job_context=job_context)))
    descriptor = discovered["data"]
    if not descriptor.get("hardwareId"):
        if not allow_start:
            raise errors.ExecutorError("AGENT_IDENTITY_BOOTSTRAP_REQUIRED", "The Local Agent identity file is absent")
        path = Path(job["directories"]["temporary"]) / "agent-before-identity.json"
        fileio.atomic_write_json(path, discovered)
        record(job, stages, 6, "bootstrap-agent-identity", agent_host.bootstrap_agent_identity(SimpleNamespace(descriptor=str(path), allow_start=True, port=[local.c_build_listener_port(), 9091, 9092, 9093, 9094, 12345], timeout=20, log=str(Path(job["directories"]["logs"]) / "agent-identity-bootstrap.log"), output=None)))
        discovered = record(job, stages, 6, "rediscover-agent-identity", agent_host.discover_agent(SimpleNamespace(agent_root=descriptor["agentRoot"], server_url=agent_server_origin, service_name=None, job_context=job_context)))
        descriptor = discovered["data"]
    _register_if_absent(job, stages, record, descriptor, cover_user_name)
    expected: dict[str, Path] = {}
    mapping_kinds = None
    if required_wrappers or real_tools_override:
        expected = local.required_real_tools(descriptor, required_wrappers, real_tools_override)
        server_agent = _get_agent(job, descriptor["hardwareId"])
        if original_environment is not None and server_agent:
            expected, mapping_kinds = _reuse_original_tool_mappings(
                server_agent, expected, original_environment, allow_wrapper_aliases=allow_wrapper_aliases,
            )
            real_tools_override = expected
            required_wrappers = set(expected)
        if not server_agent or not _server_mappings_match(server_agent, expected, required_wrappers, mapping_kinds=mapping_kinds, environment=original_environment):
            _update_agent_toolchain_mappings(job, stages, record, descriptor, required_wrappers, real_tools_override,
                                            capability, toolchain_selection, mapping_kinds, original_environment)
    listener = local.build_listener_port(capability)
    if listener not in agent_host.ports_in_use([listener]):
        if not allow_start:
            raise errors.ExecutorError("AGENT_START_REQUIRED", "The discovered Local Agent is stopped")
        descriptor_path = Path(job["directories"]["temporary"]) / "agent.json"
        fileio.atomic_write_json(descriptor_path, discovered)
        record(job, stages, 7, "start-agent", agent_host.start_agent(SimpleNamespace(descriptor=str(descriptor_path), allow_start=True, port=[listener, 9091, 9092, 9093, 9094, 12345], timeout=60)))
        discovered = record(job, stages, 8, "rediscover-agent", agent_host.discover_agent(SimpleNamespace(agent_root=descriptor["agentRoot"], server_url=agent_server_origin, service_name=None, job_context=job_context)))
        descriptor = discovered["data"]
    record(job, stages, 8, "verify-agent-runtime-open-api", _verify_runtime(job, descriptor, expected, required_wrappers, capability))
    if original_environment is not None:
        descriptor["resolvedWrapperMappings"] = {name: str(path) for name, path in expected.items()}
    return descriptor
