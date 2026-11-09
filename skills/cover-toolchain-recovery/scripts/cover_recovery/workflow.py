"""서버 TMPA 검증 상태를 보존한다. 툴체인·Wrapper 반영은 deployment.py에서 수행한다."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from .common import RecoveryError, digest_json, read_json, sha256_file, utc_now, write_json


def load_case(path: str | Path) -> dict:
    target = Path(path).resolve()
    case = read_json(target)
    if Path(case.get("run_dir", "")).resolve() != target.parent:
        raise RecoveryError("CasePathMismatch", "case.json 위치와 run_dir가 일치하지 않습니다.")
    if case.get("schema_version") != 1:
        raise RecoveryError("CaseVersion", "지원하지 않는 실행 기록 형식입니다.")
    return case


@contextmanager
def case_lock(case: dict):
    path = Path(case["run_dir"]) / ".operation.lock"
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RecoveryError("CaseBusy", "다른 작업 또는 중단된 작업의 잠금이 있습니다. 실행 상태부터 확인하세요.") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        # 잠금을 기다리는 동안 다른 작업이 저장한 최신 상태를 기준으로 판단.
        latest = load_case(Path(case["run_dir"]) / "case.json")
        case.clear()
        case.update(latest)
        yield
    finally:
        path.unlink(missing_ok=True)


def save_case(case: dict) -> None:
    root = Path(case["run_dir"])
    case["updated_at"] = utc_now()
    write_json(root / "case.json", case)
    write_report(case)


def write_report(case: dict) -> None:
    fence = chr(96) * 3
    lines = [
        "# COVER 분석 오류 복구 기록", "",
        f"- 대상 소스: {case.get('source_path', '미지정')}",
        f"- 현재 상태: **{case.get('status', 'Unknown')}**", "",
        "| 확인 단계 | 상태 |", "|---|---|",
        f"| PA 검증 | {case.get('pa_status', 'NotStarted')} |",
        f"| 서버 반영 | {case.get('server_status', 'NotStarted')} |",
        f"| 재빌드 인계 | {case.get('handoff_status', 'NotReady')} |", "",
        "## 확인한 증거", "",
        fence + "json", json.dumps(case.get("evidence", {}), ensure_ascii=False, indent=2), fence, "",
        "## 시도 기록", "",
    ]
    for index, attempt in enumerate(case.get("attempts", []), 1):
        lines.extend([
            f"### 시도 {index}", "",
            f"- 상태: {attempt.get('result', {}).get('status', 'Prepared')}",
            f"- 변경 근거: {attempt.get('proposal', {}).get('reason', '')}",
            f"- 변경 비교: {attempt.get('diff_path', '')}",
            f"- 검증 폴더: {attempt.get('workspace', '')}", "",
        ])
    blockers = list(case.get("blockers", []))
    if case.get("last_error"):
        blockers.append(case["last_error"].get("message", "작업 중단"))
    if blockers:
        lines += ["## 남은 조건", ""] + [f"- {item}" for item in blockers] + [""]
    lines += ["## PA 실행 위치", "", "- COVER 서버의 tmpa-run API", ""]
    for attempt in case.get("attempts", []):
        remote = attempt.get("remote", {})
        if remote:
            lines += [f"- runId: {remote.get('run_id', '응답 미확인')}",
                      f"- 서버 상태: {remote.get('status', remote.get('submission'))}",
                      f"- tmpa 버전: {remote.get('tmpa_revision') or '미확인'}", ""]
    lines += [
        "이 기록은 네이티브 빌드, 계측 컴파일, 커버리지 수집 성공을 뜻하지 않습니다.",
        "실제 재빌드는 호출한 스킬이 수행해야 합니다.", "",
    ]
    (Path(case["run_dir"]) / "report.md").write_text("\n".join(lines), encoding="utf-8")


def begin_case(artifacts: Path, run_dir: Path, source: str) -> dict:
    from .workspace import start_case
    case = start_case(artifacts, run_dir, source)
    case.update({
        "schema_version": 1, "created_at": utc_now(), "attempts": [],
        "execution_mode": "server", "pa_status": "NotStarted", "server_status": "NotStarted",
        "handoff_status": "NotReady",
    })
    case["status"] = "VerificationBlocked" if case.get("blockers") else "Ready"
    save_case(case)
    return case


def ensure_originals(case: dict) -> None:
    root = Path(case["run_dir"]).resolve()
    input_root = Path(case["input_root"]).resolve()
    if input_root != root / "inputs":
        raise RecoveryError("UnsafePath", "보존 입력은 해당 실행 폴더의 inputs여야 합니다.")
    for path, expected in case.get("original_hashes", {}).items():
        if not Path(path).is_file() or sha256_file(path) != expected:
            raise RecoveryError("OriginalChanged", "원본 자료가 변경됐습니다. 새 자료로 다시 시작하세요.")
    for relative, expected in case.get("input_hashes", {}).items():
        path = input_root / relative
        if not path.resolve().is_relative_to(input_root):
            raise RecoveryError("UnsafePath", "입력 해시 경로가 입력 폴더 밖에 있습니다.")
        if not path.is_file() or sha256_file(path) != expected:
            raise RecoveryError("InputChanged", "보존한 입력 복사본이 변경됐습니다.")
    actual_names = set()
    for directory, dirs, names in os.walk(input_root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RecoveryError("InputChanged", "보존 입력에 링크 또는 재분석 지점이 생겼습니다.")
            if path.is_file():
                actual_names.add(path.relative_to(input_root).as_posix())
    if actual_names != set(case.get("input_hashes", {})):
        raise RecoveryError("InputChanged", "보존 입력의 파일 목록이 변경됐습니다.")


def verify_case(case_path: Path, candidate_path: Path, *, client=None,
                timeout_seconds: int = 60, platform: str | None = None) -> dict:
    from .workspace import prepare_attempt
    from .pa import run_pa
    case = load_case(case_path)
    with case_lock(case):
        if case.get("server_status") != "NotStarted":
            raise RecoveryError("CaseAlreadyPublished", "서버 반영을 시작한 사례에는 새 후보를 적용하지 않습니다.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 60:
            raise RecoveryError("InvalidTimeout", "한 번의 조회 대기는 1~60초입니다. 서버 실행 제한은 30분입니다.")
        ensure_originals(case)
        collection = case.get("collection", {})
        if (client is None or not collection.get("server_url") or not collection.get("hardware_id")
                or collection.get("source_path") != case["source_path"]
                or collection.get("selection") != "server_latest_by_mac_and_source_path"):
            case.update(status="NeedsInput", pa_status="NotRun")
            case["last_error"] = {"code": "ServerIssueRequired", "message":
                "서버 Issue 자료가 필요합니다. start 또는 collect로 서버·HardwareID·소스를 확인해 새 case를 수집하세요."}
            save_case(case)
            return case
        if platform is not None and platform not in {"win", "linux"}:
            raise RecoveryError("InvalidInput", "platform은 빌드 PC의 win 또는 linux입니다.")
        previous = case["attempts"][-1] if case["attempts"] else {}
        remote = previous.get("remote", {})
        resume = (remote.get("pending") or remote.get("pending_archive")
                  or remote.get("submission") == "Accepted" and previous.get("result", {}).get("mode") != "server")
        if not resume and len(case["attempts"]) >= 3:
            raise RecoveryError("AttemptLimit", "최대 3회에 도달했습니다. 결과와 추가 조치를 검토하세요.")
        candidate = read_json(candidate_path)
        if not isinstance(candidate.get("replacements", []), list) or not all(
            isinstance(edit, dict) and isinstance(edit.get("new"), str)
            for edit in candidate.get("replacements", [])
        ) or not isinstance(candidate.get("append_lines", []), list):
            raise RecoveryError("InvalidCandidate", "후보의 replacements와 append_lines 형식을 확인하세요.")
        signature = digest_json({
            "replacements": candidate.get("replacements", []),
            "append_lines": candidate.get("append_lines", []),
        })
        for completed in ([] if resume else case["attempts"]):
            if completed.get("candidate_signature") == signature and completed.get("result", {}).get("status") != "VerificationBlocked":
                raise RecoveryError("RepeatedCandidate", "이미 검증한 동일 설정 변경입니다.")
        changed_lines = [edit.get("new", "") for edit in candidate.get("replacements", [])]
        changed_lines += candidate.get("append_lines", [])
        risky = ("cs_replace_code", "cs_ignore_", "cs_disable_keyword", "cs_allow_")
        if any(str(line).strip().startswith(risky) for line in changed_lines):
            raise RecoveryError("UnsupportedSemanticChange", "분석 의미를 제거하거나 느슨하게 만들 수 있는 옵션은 자동 검증 대상이 아닙니다.")
        if resume:
            attempt = previous
            if attempt.get("candidate_signature") != signature:
                raise RecoveryError("RunPending", "기존 서버 실행을 같은 후보로 먼저 조회하세요.")
            saved_platform = remote.get("request", {}).get("platform")
            if platform is None:
                platform = saved_platform
        else:
            attempt = prepare_attempt(case, candidate)
            attempt["candidate_signature"] = signature
            attempt.setdefault("proposal", candidate)
            attempt["result"] = {"status": "Prepared"}
            case["attempts"].append(attempt)
            attempt["verification_manifest_sha256"] = sha256_file(Path(attempt["workspace"]) / "verification.json")
        case["execution_mode"] = "server"
        case["status"] = "Verifying"
        case["pa_status"] = "Running"
        case.pop("publish_plan_digest", None)
        case.pop("last_error", None)
        save_case(case)
        try:
            result = run_pa(case, attempt, client, save=save_case,
                            timeout_seconds=timeout_seconds, platform=platform)
            ensure_originals(case)
        except RecoveryError as exc:
            result = {"status": "VerificationBlocked", "error": exc.as_dict()}
        except (OSError, ValueError, TypeError):
            result = {"status": "VerificationBlocked", "error": {
                "code": "VerificationInterrupted", "message": "PA 결과 또는 파일 상태를 확인하지 못했습니다."}}
        if not isinstance(result, dict) or result.get("status") not in {
            "Recovered", "RecoveryFailed", "VerificationBlocked", "Timeout"
        }:
            result = {"status": "VerificationBlocked", "error": {
                "code": "InvalidPaResult", "message": "검증 결과 형식이 올바르지 않습니다."}}
        attempt["result"] = result
        case["pa_status"] = result.get("status", "VerificationBlocked")
        case["status"] = case["pa_status"]
        save_case(case)
    return case


def verified_attempt(case: dict) -> dict:
    ensure_originals(case)
    if case.get("pa_status") != "Recovered" or not case.get("attempts"):
        raise RecoveryError("PaNotVerified", "PA 검증 성공 전에는 서버에 반영할 수 없습니다.")
    attempt = case["attempts"][-1]
    if attempt.get("result", {}).get("status") != "Recovered":
        raise RecoveryError("PaNotVerified", "최근 시도가 PA 검증에 성공하지 않았습니다.")
    required = ("workspace", "conf_path", "conf_sha256", "portable_conf_path", "portable_conf_sha256")
    if any(not isinstance(attempt.get(key), str) or not attempt[key] for key in required):
        raise RecoveryError("MissingPortableConfig", "후보 사본·서버 전송 설정의 검증 기록이 모두 필요합니다.")
    workspace = Path(attempt["workspace"]).resolve()
    conf = Path(attempt["conf_path"]).resolve()
    portable = Path(attempt["portable_conf_path"]).resolve()
    if (not workspace.is_relative_to(Path(case["run_dir"]).resolve() / "attempts")
            or not conf.is_relative_to(workspace)
            or portable.parent != workspace.parent or portable == conf):
        raise RecoveryError("UnsafeConfig", "후보 사본과 서버 전송 설정의 위치가 올바르지 않습니다.")
    for path, digest in ((conf, attempt["conf_sha256"]), (portable, attempt["portable_conf_sha256"])):
        if not path.is_file() or sha256_file(path) != digest:
            raise RecoveryError("VerifiedConfigChanged", "서버 검증 후 후보 사본 또는 전송 설정이 변경됐습니다.")
    if (attempt.get("compiler_id") != case.get("compiler_id")
            or attempt.get("selected_tu_ids") != case.get("selected_tu_ids")):
        raise RecoveryError("VerifiedCaseChanged", "검증한 컴파일러·분석 대상 기록과 현재 사례가 다릅니다.")
    manifest = workspace / "verification.json"
    if (not manifest.is_file() or not attempt.get("verification_manifest_sha256")
            or sha256_file(manifest) != attempt["verification_manifest_sha256"]):
        raise RecoveryError("VerifiedCaseChanged", "PA 검증에 사용한 입력·대상 기록이 변경됐습니다.")
    result = attempt["result"]
    remote = attempt.get("remote", {})
    from .evidence import read_preserved_text
    text, _, _ = read_preserved_text(portable)
    request = remote.get("request", {})
    collection = case.get("collection", {})
    from .cover_api import CoverOpenApiClient
    try:
        CoverOpenApiClient._run_id(remote.get("run_id"))
    except RecoveryError:
        raise RecoveryError("PaEvidenceMismatch", "검증된 서버 runId가 없습니다.") from None
    if (result.get("mode") != "server" or result.get("server_status") != "SUCCEEDED"
            or remote.get("status") != "SUCCEEDED" or remote.get("pending")
            or result.get("run_id") != remote.get("run_id")
            or result.get("conf_sha256") != attempt["portable_conf_sha256"]
            or result.get("request_digest") != digest_json(request)
            or remote.get("request_digest") != digest_json(request)
            or remote.get("server_url") != collection.get("server_url")
            or request.get("macAddress") != collection.get("hardware_id")
            or request.get("sourceFilePath") != case["source_path"]
            or request.get("compilerId") != case["compiler_id"]
            or request.get("toolChainContent") != text.splitlines()):
        raise RecoveryError("PaEvidenceMismatch", "서버 검증의 실행·설정·대상 기록이 현재 자료와 다릅니다.")
    return attempt
