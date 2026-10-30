"""Publish verified recovery settings and record the server operation."""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path

from .common import RecoveryError, decode_bytes, digest_json, read_json, sha256_file, utc_now, write_json
from .workflow import case_lock, load_case, save_case, verified_attempt

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_PUBLICATION_GUIDE = _SKILL_ROOT / "references/server-publication.md"


def _lines(path: Path) -> list[str]:
    text, _ = decode_bytes(path.read_bytes())
    return text.splitlines()


def _effective(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith(("#", ";", "//"))]


def _option(lines: list[str], name: str) -> str | None:
    values = [match.group(1).strip() for line in _effective(lines)
              if (match := re.fullmatch(re.escape(name) + r"\s*=\s*(.*)", line))]
    if len(values) > 1:
        raise RecoveryError("UnclearToolchainMetadata", f"{name} 값이 여러 개라 표시 정보를 확정할 수 없습니다.")
    return values[0] if values else None


def _toolchain_metadata(toolchain: dict) -> dict:
    """Keep the public identity/display fields; content comes from the verified copy."""
    fields = ("id", "aliasName", "majorKind", "minorKind", "majorVersion", "minorVersion",
              "targetKind", "original")
    return {key: copy.deepcopy(toolchain[key]) for key in fields}


def _proposed_toolchain(old: dict, lines: list[str], conf_hash: str,
                        name_suffix: str) -> tuple[dict, dict]:
    """Derive GCC display metadata only from the already verified configuration."""
    if not isinstance(name_suffix, str) or not re.fullmatch(r"[0-9a-f]{8}", name_suffix):
        raise RecoveryError("InvalidPublishPlan", "새 표시 이름의 식별값을 확인하세요. 계획을 다시 생성해야 합니다.")
    proposed = copy.deepcopy(old)
    proposed.pop("id", None)
    proposed["original"] = False
    proposed["toolChainContent"] = lines
    label = old["aliasName"]
    evidence = {"source": "server_metadata", "version_fields_changed": False}
    if old["majorKind"].casefold() == "gcc":
        value = _option(lines, "gnu_version")
        if value is None or not re.fullmatch(r"[1-9][0-9]{4,6}", value):
            raise RecoveryError("UnclearToolchainMetadata", "검증 설정의 gnu_version을 숫자 하나로 확인해야 합니다.")
        major, remainder = divmod(int(value), 10000)
        minor, patch = divmod(remainder, 100)
        proposed.update(majorVersion=major, minorVersion=minor)
        label = f"GCC {major}.{minor}.{patch}"
        target = _option(lines, "target")
        if target is not None:
            if not re.fullmatch(r"[A-Za-z0-9_+.-]+", target):
                raise RecoveryError("UnclearToolchainMetadata", "검증 설정의 target 표시값을 확정할 수 없습니다.")
            label += f" {target}"
        evidence = {
            "source": "verified_config", "gnu_version": int(value), "target": target,
            "version_fields_changed": (old["majorVersion"], old["minorVersion"]) != (major, minor),
        }
    proposed["aliasName"] = f"{label} recovery-{conf_hash[:10]}-{name_suffix}"
    return proposed, evidence


def _write_rollback_record(case: dict, operation: dict) -> None:
    """Record a manual rollback handoff, never execute or replay remote writes."""
    write_json(Path(case["run_dir"]) / "rollback.json", {
        "status": "NotExecuted", "requires_state_verification": True,
        "plan_digest": case["publish_plan_digest"],
        "server_url": operation["server_url"], "agent_id": operation["agent_id"],
        "hardware_id": operation["hardware_id"], "wrapper_name": operation["wrapper_name"],
        "publication_status": case["server_status"],
        "created_toolchain_id": case.get("created_toolchain_id"),
        "created_toolchain_alias": operation["proposed_toolchain"]["aliasName"],
        "restore_toolchain_id": operation["previous_toolchain_id"],
        "restore_toolchain_alias": operation["previous_toolchain"]["aliasName"],
        "baseline_plan_path": str(Path(case["run_dir"]) / "publish-plan.json"),
        "procedure_path": str(_PUBLICATION_GUIDE),
        "restore_scope": "selected_wrapper_mapping_only",
        "delete_toolchains": False,
    })


def _check_no_handoff(case: dict) -> None:
    if (Path(case["run_dir"]) / "handoff.json").exists():
        raise RecoveryError("UnexpectedHandoff", "기존 인계 기록이 있습니다. 현재 단계와 파일을 먼저 확인하세요.")


def _planned_operation(case: dict, *, server_url: str, plan_digest: str) -> tuple[dict, dict]:
    if case.get("server_status") != "NotStarted":
        raise RecoveryError("PublishAlreadyStarted", "중복 반영하지 않습니다. 서버를 재조회하고 기록된 상태를 확인하세요.")
    _check_no_handoff(case)
    attempt = verified_attempt(case)
    plan = read_json(Path(case["run_dir"]) / "publish-plan.json")
    operation = plan.get("operation")
    if not isinstance(operation, dict) or operation.get("schema_version") != 3:
        raise RecoveryError("InvalidPublishPlan", "현재 형식으로 publish-plan을 다시 생성하고 변경 범위를 확인하세요.")
    digest = digest_json(operation)
    if not plan_digest or plan_digest != digest or digest != case.get("publish_plan_digest"):
        raise RecoveryError("PlanMismatch", "생성한 반영 계획과 일치하지 않습니다.")
    if operation.get("server_url") != server_url:
        raise RecoveryError("ServerMismatch", "반영 계획의 COVER 서버 주소와 다릅니다.")
    return attempt, operation


def validate_publish_request(case_path: Path, *, server_url: str, plan_digest: str) -> None:
    """Check the planned destination before the CLI reads the Access key."""
    case = load_case(case_path)
    with case_lock(case):
        _planned_operation(case, server_url=server_url, plan_digest=plan_digest)


def prepare_publish(case_path: Path, client, *, server_url: str, hardware_id: str,
                    wrapper_name: str) -> dict:
    case = load_case(case_path)
    with case_lock(case):
        if case.get("server_status") != "NotStarted":
            raise RecoveryError("PublishAlreadyStarted", "이미 서버 반영을 시작했습니다. 현재 상태를 먼저 확인하세요.")
        _check_no_handoff(case)
        attempt = verified_attempt(case)
        logical = Path(attempt["portable_conf_path"])
        if not logical.resolve().is_relative_to(Path(case["run_dir"]).resolve()):
            raise RecoveryError("UnsafeConfig", "배포 설정이 실행 폴더 밖에 있습니다.")
        expected_hash = attempt["portable_conf_sha256"]
        if sha256_file(logical) != expected_hash:
            raise RecoveryError("VerifiedConfigChanged", "검증한 배포 설정이 변경됐습니다.")
        lines = _lines(logical)
        run_text = str(Path(case["run_dir"]).resolve()).replace("\\", "/").lower()
        if any(run_text in line.replace("\\", "/").lower() for line in lines):
            raise RecoveryError("NonportableConfig", "검증 폴더의 임시 경로가 포함된 설정은 서버에 반영하지 않습니다.")
        agent = client.find_agent(hardware_id)
        wrappers = [item for item in agent["toolChains"] if item.get("wrapperName") == wrapper_name]
        if len(wrappers) != 1 or wrappers[0].get("toolKind") not in {"C_COMPILER", "CPP_COMPILER", "C_CPP_COMPILER"}:
            raise RecoveryError("AmbiguousTarget", "C/C++ Wrapper 하나를 정확히 지정해야 합니다.")
        old = _toolchain_metadata(client.get_toolchain(wrappers[0]["toolChainInfo"]["id"]))
        # Publish the verified copy as a new definition. The old definition is retained
        # for its metadata and rollback ID, so reading/comparing its body is unnecessary.
        name_suffix = uuid.uuid4().hex[:8]
        proposed, metadata_evidence = _proposed_toolchain(old, lines, expected_hash, name_suffix)
        operation = {
            "schema_version": 3, "server_url": server_url, "hardware_id": hardware_id,
            "agent_id": agent["id"], "wrapper_name": wrapper_name,
            "previous_toolchain_id": old["id"], "previous_toolchain": old,
            "agent_snapshot": agent, "proposed_toolchain": proposed,
            "name_suffix": name_suffix, "metadata_evidence": metadata_evidence,
            "conf_path": str(logical), "conf_sha256": expected_hash,
            "runtime_conf_sha256": attempt["conf_sha256"],
            "attempt_workspace": attempt["workspace"],
            "verification_manifest_sha256": attempt["verification_manifest_sha256"],
        }
        plan = {"operation": operation, "plan_digest": digest_json(operation), "created_at": utc_now(),
                "notice": "서버 읽기 전용 계획. 검증 결과와 대상이 일치하면 publish로 새 툴체인 생성·Wrapper 연결을 진행함."}
        case["publish_plan_digest"] = plan["plan_digest"]
        case["status"] = "ReadyToPublish"
        write_json(Path(case["run_dir"]) / "publish-plan.json", plan)
        _write_rollback_record(case, operation)
        save_case(case)
        return plan


def apply_publish(case_path: Path, client, *, server_url: str,
                  plan_digest: str) -> dict:
    case = load_case(case_path)
    with case_lock(case):
        attempt, operation = _planned_operation(
            case, server_url=server_url, plan_digest=plan_digest,
        )
        if (operation["runtime_conf_sha256"] != attempt["conf_sha256"]
                or operation["attempt_workspace"] != attempt["workspace"]
                or operation["verification_manifest_sha256"] != attempt["verification_manifest_sha256"]
                or operation["conf_path"] != attempt["portable_conf_path"]
                or operation["conf_sha256"] != attempt["portable_conf_sha256"]):
            raise RecoveryError("VerifiedConfigChanged", "반영 계획의 PA 검증 결과와 다릅니다.")
        if sha256_file(operation["conf_path"]) != operation["conf_sha256"]:
            raise RecoveryError("VerifiedConfigChanged", "반영 계획의 배포 설정이 변경됐습니다.")
        if _lines(Path(operation["conf_path"])) != operation["proposed_toolchain"].get("toolChainContent"):
            raise RecoveryError("PlanMismatch", "반영 계획의 전송 내용과 검증한 배포 설정이 다릅니다.")
        proposed, metadata_evidence = _proposed_toolchain(
            operation["previous_toolchain"], _lines(Path(operation["conf_path"])),
            operation["conf_sha256"], operation.get("name_suffix"),
        )
        if proposed != operation["proposed_toolchain"] or metadata_evidence != operation.get("metadata_evidence"):
            raise RecoveryError("PlanMismatch", "반영 계획의 이름·버전·종류가 검증 설정 및 기존 툴체인과 맞지 않습니다.")
        current = client.get_agent(operation["hardware_id"])
        # The client also checks its complete configuration snapshot immediately before PATCH.
        if current.get("id") != operation["agent_id"]:
            raise RecoveryError("StaleTarget", "MAC 주소에 연결된 서버 Agent ID가 변경됐습니다.")
        before = {key: value for key, value in operation["agent_snapshot"].items() if key not in {"online", "startDate"}}
        now = {key: value for key, value in current.items() if key not in {"online", "startDate"}}
        for obj in (before, now):
            for key in ("toolChains", "exclusionPolicies", "inclusions"):
                if isinstance(obj.get(key), list):
                    obj[key] = sorted(obj[key], key=digest_json)
        if digest_json(before) != digest_json(now):
            raise RecoveryError("StaleTarget", "계획 생성 이후 서버 설정이 변경됐습니다.")
        if _toolchain_metadata(client.get_toolchain(operation["previous_toolchain_id"])) != operation["previous_toolchain"]:
            raise RecoveryError("StaleBaseline", "계획 생성 이후 기존 툴체인의 표시 정보가 변경됐습니다.")
        # Persist intent before the first non-idempotent request. Never replay automatically.
        case["server_status"] = "Applying"
        case["status"] = "Applying"
        case["handoff_status"] = "NotReady"
        case["publication_execution"] = {"plan_digest": plan_digest, "recorded_at": utc_now()}
        save_case(case)
        try:
            _write_rollback_record(case, operation)
            created = client.create_toolchain(operation["proposed_toolchain"])
            if not isinstance(created, dict) or not isinstance(created.get("id"), str) or not created["id"]:
                raise RecoveryError("AmbiguousWrite", "새 툴체인의 ID를 확인하지 못했습니다. 재전송하지 않습니다.")
            case["created_toolchain_id"] = created["id"]
            case["server_status"] = "ToolchainCreated"
            save_case(case)
            _write_rollback_record(case, operation)
            case["server_status"] = "MappingUpdating"
            save_case(case)
            client.update_agent_mapping(
                operation["agent_id"], operation["hardware_id"], operation["wrapper_name"],
                created["id"], operation["previous_toolchain_id"],
                expected_snapshot=operation["agent_snapshot"],
            )
            case["server_status"] = "ServerApplied"
            save_case(case)
            write_json(Path(case["run_dir"]) / "handoff.json", {
                "status": "ReadyForRebuild", "server_url": server_url,
                "hardware_id": operation["hardware_id"], "source_path": case["source_path"],
                "toolchain_id": created["id"], "wrapper_name": operation["wrapper_name"],
                "report_path": str(Path(case["run_dir"]) / "report.md"),
                "rollback_path": str(Path(case["run_dir"]) / "rollback.json"),
                "rebuild_executed": False, "coverage_verified": False,
            })
            case["handoff_status"] = "ReadyForRebuild"
            case["status"] = "ReadyForRebuild"
        except (RecoveryError, OSError, ValueError, TypeError, KeyError) as exc:
            case["last_error"] = exc.as_dict() if isinstance(exc, RecoveryError) else {
                "code": "PublicationInterrupted", "message": "반영 결과를 완전히 확인하지 못했습니다. 자동 재전송하지 않습니다."
            }
            case["status"] = "NeedsAttention"
            if case["server_status"] == "Applying":
                case["server_status"] = "Unknown"
            elif case["server_status"] == "MappingUpdating":
                known_before_write = isinstance(exc, RecoveryError) and exc.code in {
                    "StaleTarget", "TargetNotFound", "AmbiguousTarget", "UnsupportedMapping",
                    "InvalidInput", "InvalidApiResponse", "ApiNetworkError", "ApiRedirect", "UnsafeRedirect",
                    "ApiRejected", "AccessKeyRequired",
                }
                case["server_status"] = "ToolchainCreated" if known_before_write else "Unknown"
            case["handoff_status"] = "NotReady"
        save_case(case)
        _write_rollback_record(case, operation)
    return case
