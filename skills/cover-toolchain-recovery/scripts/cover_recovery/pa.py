"""Run TMPA through COVER's asynchronous server API."""
from __future__ import annotations

import time
import zipfile
from pathlib import Path

from .common import RecoveryError, digest_json, sha256_file, utc_now, write_json
from .evidence import read_preserved_text
from .workspace import _safe_zip


def run_pa(case: dict, attempt: dict, client, *, save, timeout_seconds: int = 60,
           platform: str | None = None) -> dict:
    """Persist POST intent and runId before polling; resuming never resubmits a run."""
    conf = Path(attempt["portable_conf_path"])
    if sha256_file(conf) != attempt["portable_conf_sha256"]:
        raise RecoveryError("VerifiedConfigChanged", "검증 설정이 변경됐습니다.")
    content, _, _ = read_preserved_text(conf)
    collection = case["collection"]
    request = {"sourceFilePath": case["source_path"], "macAddress": collection["hardware_id"],
               "compilerId": case["compiler_id"], "toolChainContent": content.splitlines()}
    if platform is not None:
        request["platform"] = platform
    remote = attempt.get("remote")
    if remote:
        if (remote.get("request_digest") != digest_json(request)
                or remote.get("server_url") != collection["server_url"]):
            raise RecoveryError("RunInputChanged", "진행 중인 실행의 입력을 바꿀 수 없습니다.")
        if not remote.get("run_id"):
            raise RecoveryError("AmbiguousWrite", "실행 ID를 받지 못한 요청입니다. 서버에서 실행 여부를 확인하세요.")
    else:
        remote = {"mode": "server", "server_url": collection["server_url"],
                  "request": request, "request_digest": digest_json(request),
                  "submitted_at": utc_now(), "submission": "Submitting", "pending": True}
        attempt["remote"] = remote
        save(case)
        try:
            remote["run_id"] = client.start_tmpa_run(
                case["source_path"], collection["hardware_id"], case["compiler_id"],
                request["toolChainContent"], platform=platform)
        except RecoveryError as exc:
            # The API guarantees no run for a completed HTTP 4xx/5xx response.
            if exc.code in {"ApiHttpError", "TMPA_RUN_BUSY", "ISSUE_OUTDATED", "AuthenticationFailed",
                            "PermissionDenied", "InvalidInput", "AccessKeyRequired"}:
                remote.update(submission="Rejected", pending=False)
            else:
                remote["submission"] = "Unknown"
            save(case)
            raise
        remote["submission"] = "Accepted"
        save(case)

    deadline = time.monotonic() + timeout_seconds
    while True:
        # A persisted terminal response remains evidence after server retention expires.
        status = ({"status": remote["status"], "tmpaRevision": remote.get("tmpa_revision")}
                  if remote.get("status") in {"SUCCEEDED", "FAILED", "TIMEOUT", "ERROR"}
                  else client.get_tmpa_run(remote["run_id"]))
        remote.update(status=status["status"], tmpa_revision=status.get("tmpaRevision"),
                      checked_at=utc_now(), pending=status["status"] == "RUNNING")
        save(case)
        if status["status"] != "RUNNING":
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "VerificationBlocked", "mode": "server", "run_id": remote["run_id"],
                    "reason": "PollingPending", "message": "서버 실행은 계속됩니다. 같은 verify 명령으로 조회를 재개하세요."}
        time.sleep(min(2, remaining))

    outcome = {"SUCCEEDED": "Recovered", "FAILED": "RecoveryFailed",
               "TIMEOUT": "Timeout", "ERROR": "VerificationBlocked"}[status["status"]]
    result = {"status": outcome, "mode": "server", "run_id": remote["run_id"],
              "server_status": status["status"], "tmpa_revision": status.get("tmpaRevision"),
              "request_digest": remote["request_digest"], "conf_sha256": attempt["portable_conf_sha256"],
              "evidence_source": "COVER tmpa-run status API"}
    if status["status"] in {"FAILED", "TIMEOUT"}:
        archive = Path(attempt["workspace"]).parent / "error-archive.zip"
        try:
            if not archive.exists():
                data = client.download_tmpa_error_archive(remote["run_id"])
                with archive.open("xb") as output:
                    output.write(data)
            result["error_archive"] = str(archive)
            result["error_archive_sha256"] = sha256_file(archive)
            extracted = archive.with_name("error-artifacts")
            if not extracted.exists():
                _safe_zip(archive, extracted)
            result["error_artifacts"] = str(extracted)
            result["diagnostic_only"] = status["status"] == "TIMEOUT"
        except (RecoveryError, OSError, ValueError, zipfile.BadZipFile) as exc:
            result["archive_error"] = (exc.as_dict() if isinstance(exc, RecoveryError) else
                                       {"code": "ArchiveSaveFailed", "message": "오류 ZIP을 저장하지 못했습니다."})
            remote["pending_archive"] = True
        else:
            remote["pending_archive"] = False
    write_json(Path(attempt["workspace"]).parent / "server-result.json", result)
    return result
