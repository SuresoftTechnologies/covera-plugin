"""로컬 검증 상태를 보존한다. 서버 변경은 deployment.py에서만 수행한다."""

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
    runtime = case.get("runtime", {})
    if runtime:
        lines += ["## PA 실행 파일", "", f"- 확인한 위치: {case.get('pa_root') or runtime.get('root', '')}",
                  f"- 실행 파일: {runtime.get('executable') or '찾지 못함'}", ""]
        if not runtime.get("available"):
            lines += ["PA를 사용할 수 없어 진단·변경안 안내만 가능합니다. 스킬의 tmpa 배포 파일 또는 명시한 PA 경로를 확인하세요.", ""]
            for blocker in runtime.get("blockers", []):
                lines.append(f"- {blocker.get('code', '')}: {blocker.get('message', '')}"
                             if isinstance(blocker, dict) else f"- {blocker}")
            lines.append("")
    lines += [
        "이 기록은 네이티브 빌드, 계측 컴파일, 커버리지 수집 성공을 뜻하지 않습니다.",
        "실제 재빌드는 호출한 스킬이 수행해야 합니다.", "",
    ]
    (Path(case["run_dir"]) / "report.md").write_text("\n".join(lines), encoding="utf-8")


def begin_case(artifacts: Path, run_dir: Path, source: str,
               pa_root: Path | None = None, executable: Path | None = None) -> dict:
    from .workspace import start_case
    from .pa import inspect_runtime, resolve_runtime_paths
    case = start_case(artifacts, run_dir, source)
    runtime_root, executable = resolve_runtime_paths(pa_root, executable)
    case.update({
        "schema_version": 1, "created_at": utc_now(), "attempts": [],
        "pa_root": str(runtime_root),
        "pa_executable": str(executable) if executable else None,
        "pa_status": "NotStarted", "server_status": "NotStarted",
        "handoff_status": "NotReady",
    })
    runtime = inspect_runtime(runtime_root, executable)
    case["runtime"] = runtime
    case["status"] = (
        "GuidanceOnly" if not runtime.get("available") else
        "VerificationBlocked" if case.get("blockers") else "Ready"
    )
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


def verify_case(case_path: Path, candidate_path: Path, *, pa_root: Path | None = None,
                executable: Path | None = None, timeout_seconds: int = 600) -> dict:
    from .workspace import prepare_attempt
    from .pa import inspect_runtime, resolve_runtime_paths, run_pa
    case = load_case(case_path)
    with case_lock(case):
        if case.get("server_status") != "NotStarted":
            raise RecoveryError("CaseAlreadyPublished", "서버 반영을 시작한 사례에는 새 후보를 적용하지 않습니다.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 600:
            raise RecoveryError("InvalidTimeout", "회당 실행 시간은 1~600초입니다.")
        ensure_originals(case)
        if pa_root is not None or executable is not None:
            root, exe = resolve_runtime_paths(pa_root, executable)
        else:
            root, exe = resolve_runtime_paths(case.get("pa_root"), case.get("pa_executable"))
        runtime = inspect_runtime(root, exe)
        case["runtime"] = runtime
        case["pa_root"] = str(root)
        case["pa_executable"] = str(exe) if exe else None
        if not runtime.get("available"):
            case["status"] = "GuidanceOnly"
            case["pa_status"] = "NotRun"
            save_case(case)
            return case
        if len(case["attempts"]) >= 3:
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
        for previous in case["attempts"]:
            if previous.get("candidate_signature") == signature and previous.get("result", {}).get("status") != "VerificationBlocked":
                raise RecoveryError("RepeatedCandidate", "이미 검증한 동일 설정 변경입니다.")
        changed_lines = [edit.get("new", "") for edit in candidate.get("replacements", [])]
        changed_lines += candidate.get("append_lines", [])
        risky = ("cs_replace_code", "cs_ignore_", "cs_disable_keyword", "cs_allow_")
        if any(str(line).strip().startswith(risky) for line in changed_lines):
            raise RecoveryError("UnsupportedSemanticChange", "분석 의미를 제거하거나 느슨하게 만들 수 있는 옵션은 자동 검증 대상이 아닙니다.")
        attempt = prepare_attempt(case, candidate)
        attempt["candidate_signature"] = signature
        attempt.setdefault("proposal", candidate)
        attempt["result"] = {"status": "Prepared"}
        case["attempts"].append(attempt)
        case["status"] = "Verifying"
        case["pa_status"] = "Running"
        case.pop("publish_plan_digest", None)
        case.pop("last_error", None)
        attempt["verification_manifest_sha256"] = sha256_file(Path(attempt["workspace"]) / "verification.json")
        save_case(case)
        try:
            if not attempt.get("path_audit", {}).get("safe"):
                result = {"status": "VerificationBlocked",
                          "blockers": attempt.get("path_audit", {}).get("blockers", [])}
            else:
                result = run_pa(Path(attempt["workspace"]), runtime, Path(attempt["conf_path"]),
                                int(case["compiler_id"]), timeout_seconds=timeout_seconds)
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
        raise RecoveryError("MissingPortableConfig", "실행용·배포용 설정의 검증 기록이 모두 필요합니다.")
    workspace = Path(attempt["workspace"]).resolve()
    conf = Path(attempt["conf_path"]).resolve()
    portable = Path(attempt["portable_conf_path"]).resolve()
    if (not workspace.is_relative_to(Path(case["run_dir"]).resolve() / "attempts")
            or not conf.is_relative_to(workspace)
            or portable.parent != workspace.parent or portable == conf):
        raise RecoveryError("UnsafeConfig", "실행용 설정과 별도 배포용 설정의 위치가 올바르지 않습니다.")
    for path, digest in ((conf, attempt["conf_sha256"]), (portable, attempt["portable_conf_sha256"])):
        if not path.is_file() or sha256_file(path) != digest:
            raise RecoveryError("VerifiedConfigChanged", "PA 검증 후 실행용 또는 배포용 설정이 변경됐습니다.")
    if (not attempt.get("path_audit", {}).get("safe")
            or attempt.get("compiler_id") != case.get("compiler_id")
            or attempt.get("selected_tu_ids") != case.get("selected_tu_ids")):
        raise RecoveryError("VerifiedCaseChanged", "검증한 컴파일러·분석 대상 기록과 현재 사례가 다릅니다.")
    manifest = workspace / "verification.json"
    if (not manifest.is_file() or not attempt.get("verification_manifest_sha256")
            or sha256_file(manifest) != attempt["verification_manifest_sha256"]):
        raise RecoveryError("VerifiedCaseChanged", "PA 검증에 사용한 입력·대상 기록이 변경됐습니다.")
    result = attempt["result"]
    active = result.get("active_config", {})
    selected = result.get("analysis_evidence", {}).get("selected_tus", [])
    if (result.get("workspace") != str(workspace)
            or result.get("exit_code") != 0
            or active.get("path") != str(conf)
            or active.get("sha256") != attempt["conf_sha256"]
            or active.get("compiler_id") != case["compiler_id"]
            or active.get("database_reference_verified") is not True
            or not isinstance(selected, list)
            or any(not isinstance(item, dict) or type(item.get("tu_id")) is not int for item in selected)
            or sorted(item.get("tu_id", -1) for item in selected) != sorted(case["selected_tu_ids"])):
        raise RecoveryError("PaEvidenceMismatch", "PA 결과의 설정·컴파일러·대상 기록이 현재 검증 자료와 다릅니다.")
    return attempt
