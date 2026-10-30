#!/usr/bin/env python3
"""COVER recovery skill command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cover_recovery.common import RecoveryError
from cover_recovery.workflow import begin_case, load_case, verify_case


def _server_options(parser, *, required=True):
    parser.add_argument("--server", required=required, help="COVER 서버 루트 URL")
    parser.add_argument("--access-key-env", default="COVER_ACCESS_KEY", help="Access key를 저장한 환경변수 이름")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--insecure-tls", action="store_true",
                     help="HTTPS 인증서 검증을 생략함 (기본값)")
    tls.add_argument("--verify-tls", dest="insecure_tls", action="store_false",
                     help="명시적으로 요청한 경우 HTTPS 인증서와 hostname을 검증함")
    parser.set_defaults(insecure_tls=True)


def _runtime_options(parser):
    parser.add_argument("--pa-root", type=Path,
                        help="PA 경로 변경: PA 폴더 또는 단독 tmpa 파일. 기본은 스킬 루트의 assets/tmpa 폴더; verify는 저장된 경로 사용")
    parser.add_argument("--pa-executable", type=Path,
                        help="PA 실행 파일 직접 지정. --pa-root와 함께 쓰면 해당 폴더 기준 상대 경로도 가능")


def _discovery_options(parser):
    parser.add_argument("--project-root", type=Path, help="오류 명령을 실행한 빌드 작업 폴더")
    parser.add_argument("--agent-root", type=Path, help="환경변수 대신 사용할 Agent 설치 폴더")
    parser.add_argument("--server-root", type=Path, help="로컬 서버 설치 폴더 또는 catalina.home")
    parser.add_argument("--hardware-file", type=Path, help="빌드 계정의 HardwareID 파일")
    parser.add_argument("--hardware-id", help="빌드 Agent HardwareID; 생략하면 빌드 계정 파일에서 확인")
    parser.add_argument("--search-root", type=Path, action="append", default=[], help="추가 탐색 폴더 또는 ZIP; 반복 가능")


def _context_options(args):
    return {
        "project_root": args.project_root, "agent_root": args.agent_root,
        "server_root": args.server_root, "server_url": args.server,
        "hardware_file": args.hardware_file, "search_roots": args.search_root,
        "hardware_id": args.hardware_id,
        "access_key_env": args.access_key_env,
    }


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="COVER 분석 오류 복구: 원본 보존, 로컬 PA, 단계별 검증")
    subs = parser.add_subparsers(dest="command", required=True)
    start = subs.add_parser("start", help="오류 ZIP/폴더를 복사하고 증거와 초기 상태 기록")
    start.add_argument("--artifacts", type=Path, help="생략하면 빌드 문맥에서 오류 자료 자동 탐색")
    start.add_argument("--source", required=True)
    start.add_argument("--run-dir", required=True, type=Path)
    _runtime_options(start)
    _server_options(start, required=False)
    _discovery_options(start)
    discovery = subs.add_parser("discover", help="빌드 환경·서버 설정·오류 자료를 읽기 전용으로 탐색")
    discovery.add_argument("--source", required=True)
    _server_options(discovery, required=False)
    _discovery_options(discovery)
    collect = subs.add_parser("collect", help="Open API로 최신 PA 오류 자료를 다운로드하고 시작")
    _server_options(collect, required=False)
    _runtime_options(collect)
    _discovery_options(collect)
    collect.add_argument("--source", required=True)
    collect.add_argument("--archive", required=True, type=Path, help="새 오류 ZIP 저장 경로")
    collect.add_argument("--run-dir", required=True, type=Path)
    verify = subs.add_parser("verify", help="근거가 있는 후보를 복사본에서 PA 검증")
    verify.add_argument("--case", required=True, type=Path)
    verify.add_argument("--candidate", required=True, type=Path)
    verify.add_argument("--timeout", type=int, default=600)
    _runtime_options(verify)
    inspect = subs.add_parser("inspect", help="실행 기록 읽기")
    inspect.add_argument("--case", required=True, type=Path)
    runtime = subs.add_parser("runtime", help="스킬에 동봉된 tmpa 또는 명시한 PA 확인; 실행하지 않음")
    _runtime_options(runtime)
    plan = subs.add_parser("publish-plan", help="서버 변경 계획 생성; 서버 읽기만 수행")
    _server_options(plan)
    plan.add_argument("--case", required=True, type=Path)
    plan.add_argument("--hardware-id", required=True)
    plan.add_argument("--wrapper", required=True)
    apply = subs.add_parser("publish", help="검증된 계획으로 서버 반영")
    _server_options(apply)
    apply.add_argument("--case", required=True, type=Path)
    apply.add_argument("--plan-digest", required=True, help="publish-plan의 plan_digest")
    return parser


def _client(args):
    from cover_recovery.cover_api import CoverOpenApiClient
    client = CoverOpenApiClient(args.server, allow_http=False, insecure_tls=args.insecure_tls)
    try:
        client.access_key_from_env(args.access_key_env)
    except Exception:
        client.close()
        raise
    return client


def _collect_case(args, context: dict, archive: Path) -> dict:
    server = args.server or context["server_url"]
    hardware = args.hardware_id or context["hardware_id"]
    if not server or not hardware:
        missing = [key for key, value in (("server_url", server), ("hardware_id", hardware)) if not value]
        raise RecoveryError("MissingCollectionContext", "탐색 후에도 서버 또는 빌드 Agent를 확인하지 못했습니다.",
                            {"missing": missing, "context": context})
    archive = archive.resolve()
    if archive.exists() or args.run_dir.exists():
        raise RecoveryError("OutputExists", "기존 ZIP/실행 폴더를 덮어쓰지 않습니다.")
    if archive.is_relative_to(args.run_dir.resolve()) or args.run_dir.resolve().is_relative_to(archive):
        raise RecoveryError("UnsafePath", "원본 ZIP과 실행 폴더는 서로 분리해야 합니다.")
    args.server = server
    with _client(args) as client:
        data = client.download_issue_archive(context["source_path"], hardware)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("xb") as stream:
        stream.write(data)
    result = begin_case(archive, args.run_dir, context["source_path"], args.pa_root, args.pa_executable)
    result["collection"] = {
        "server_url": server, "hardware_id": hardware,
        "archive": str(archive), "source_path": context["source_path"],
        "api": "open_api_v1", "endpoint": "/v1/issue/download", "issue_work_type": "PA_ERROR_RESULT",
        "selection": "server_latest_by_mac_and_source_path",
        "issue_metadata_available": False,
    }
    from cover_recovery.workflow import save_case
    save_case(result)
    return result


def execute(args) -> dict:
    if args.command == "start":
        from cover_recovery.discovery import discover_artifacts, resolve_source
        source = resolve_source(args.source, args.project_root)
        if args.artifacts:
            return begin_case(args.artifacts, args.run_dir, source, args.pa_root, args.pa_executable)
        if args.run_dir.exists():
            raise RecoveryError("OutputExists", "기존 실행 폴더를 덮어쓰지 않습니다.")
        discovery = discover_artifacts(args.source, **_context_options(args))
        context = discovery["context"]
        missing = []
        for key in ("server_url", "hardware_id"):
            if not (getattr(args, key.replace("_url", ""), None) or context.get(key)):
                missing.append(key)
        if not context["credentials"]["access_key_present"]:
            missing.append("access_key_environment_variable")
        if context["http_transport_unsupported"]:
            missing.append("https_server_url")
        if discovery["status"] != "ServerLookupRequired" or missing:
            return {
                "status": "NeedsInput", "discovery": discovery, "missing": missing,
                "next_action": discovery["next_action"],
                "do_not_request": ["artifact_zip", "elog", "issue_id"],
                "input_purpose": "open_api_issue_collection",
                "must_resume": "start",
                "prohibited_fallback": "GuidanceOnly",
                "message": "오류 ZIP이나 .elog, Issue ID는 요구하지 않습니다. 서버 API 수집에 필요한 누락 조건만 확인하세요.",
            }
        archive = args.run_dir.with_name(args.run_dir.name + "-issue.zip")
        result = _collect_case(args, context, archive)
        result["discovery"] = discovery
        from cover_recovery.workflow import save_case
        save_case(result)
        return result
    if args.command == "discover":
        from cover_recovery.discovery import discover_artifacts
        return discover_artifacts(args.source, **_context_options(args))
    if args.command == "collect":
        from cover_recovery.discovery import discover_context
        context = discover_context(args.source, **_context_options(args))
        return _collect_case(args, context, args.archive)
    if args.command == "verify":
        return verify_case(args.case, args.candidate, pa_root=args.pa_root,
                           executable=args.pa_executable, timeout_seconds=args.timeout)
    if args.command == "inspect":
        return load_case(args.case)
    if args.command == "runtime":
        from cover_recovery.pa import inspect_runtime
        return inspect_runtime(args.pa_root, args.pa_executable)
    if args.command == "publish-plan":
        from cover_recovery.deployment import prepare_publish
        from cover_recovery.workflow import verified_attempt
        verified_attempt(load_case(args.case))
        with _client(args) as client:
            return prepare_publish(args.case, client, server_url=args.server,
                                   hardware_id=args.hardware_id, wrapper_name=args.wrapper)
    if args.command == "publish":
        from cover_recovery.deployment import apply_publish, validate_publish_request
        validate_publish_request(
            args.case, server_url=args.server, plan_digest=args.plan_digest,
        )
        with _client(args) as client:
            return apply_publish(args.case, client, server_url=args.server,
                                 plan_digest=args.plan_digest)
    raise RecoveryError("UnknownCommand", "지원하지 않는 명령입니다.")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = make_parser().parse_args()
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        blocked = result.get("status") in {
            "VerificationBlocked", "RecoveryFailed", "NeedsAttention", "Timeout", "Blocked", "NeedsInput"
        }
        if args.command == "runtime" and not result.get("available"):
            blocked = True
        return 2 if blocked else 0
    except RecoveryError as exc:
        print(json.dumps({"status": "Blocked", "error": exc.as_dict()}, ensure_ascii=False, indent=2))
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Do not echo environment variables, HTTP payloads, or subprocess streams.
        print(json.dumps({"status": "Blocked", "error": {
            "code": type(exc).__name__, "message": "입력 또는 파일 접근을 확인하세요."
        }}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
