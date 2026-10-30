#!/usr/bin/env python3
"""Low-level Open API v1 CLI for the COVER coverage-build skill.

The executor emits and consumes typed JSON.  It never invokes a shell for build
commands, never persists environment variables, and never accepts an API token
on the command line.

Do not start code reading here. Top-level orchestration lives in
``cover_build.workflow`` and environment flows live in
``cover_build.pipelines``. This module provides shared atomic operations used
by those focused modules.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import xml.etree.ElementTree as ET

from cover_build.common import agent_host
from cover_build.common import envelopes
from cover_build.common import gcc_tools
from cover_build.common import jobs
from cover_build.common import open_api
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.common import project_files
from cover_build.common import toolchains
from cover_build.core import errors
from cover_build.pipelines.visual_studio import planning as vs_planning


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-context", help="Integrity-protected job.json from init-job")
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("init-job")
    item.add_argument("--project-root", required=True)
    item.add_argument("--jobs-root")
    item.add_argument("--server-url", help="Local Agent remote-service URL; defaults to CODESCROLL_TM_SERVER_URL (legacy: COVER_SERVER_URL)")
    item.add_argument("--server-base-url", help="Legacy alias for the Local Agent remote-service URL; use --open-api-server-url for Open API")
    item.add_argument(
        "--open-api-server-url",
        help=(
            "COVER Open API main web server URL; falls back to CODESCROLL_TM_INSTALL_PATH/"
            "agent_kit/open-api-credentials.json when omitted. Ask the user when missing; "
            "do not derive it from CODESCROLL_TM_SERVER_URL"
        ),
    )
    tls_group = item.add_mutually_exclusive_group()
    tls_group.add_argument("--insecure-skip-tls-verify", dest="insecure_skip_tls_verify", action="store_true")
    tls_group.add_argument("--verify-tls", dest="insecure_skip_tls_verify", action="store_false")
    item.set_defaults(insecure_skip_tls_verify=None)
    item.add_argument("--token-env", default="COVER_ACCESS_KEY")
    item.set_defaults(handler=jobs.init_job)

    item = sub.add_parser("auth-preflight")
    item.add_argument("--require-public", action="store_true")
    item.add_argument("--output")
    item.set_defaults(handler=jobs.auth_preflight)

    item = sub.add_parser("discover-project")
    item.add_argument("--root", required=True)
    item.add_argument("--output")
    item.set_defaults(handler=project_files.discover_project)

    item = sub.add_parser("discover-agent")
    item.add_argument("--agent-root")
    item.add_argument("--server-url", required=True)
    item.add_argument("--service-name")
    item.add_argument("--output")
    item.set_defaults(handler=agent_host.discover_agent)

    item = sub.add_parser("start-agent")
    item.add_argument("--descriptor", required=True)
    item.add_argument("--allow-start", action="store_true")
    item.add_argument("--port", action="append", type=int, default=[9090, 9091, 9092, 9093, 9094, 12345])
    item.add_argument("--timeout", type=int, default=60)
    item.add_argument("--output")
    item.set_defaults(handler=agent_host.start_agent)

    item = sub.add_parser("bootstrap-agent-identity")
    item.add_argument("--descriptor", required=True)
    item.add_argument("--allow-start", action="store_true")
    item.add_argument("--port", action="append", type=int, default=[9090, 9091, 9092, 9093, 9094, 12345])
    item.add_argument("--timeout", type=int, default=20)
    item.add_argument("--log", required=True)
    item.add_argument("--output")
    item.set_defaults(handler=agent_host.bootstrap_agent_identity)

    item = sub.add_parser("verify-toolchain")
    item.add_argument("--agent-root", required=True)
    item.add_argument("--tool", action="append", required=True)
    item.add_argument("--output")
    item.set_defaults(handler=toolchains.verify_toolchain)

    item = sub.add_parser("create-vs-overlay")
    item.add_argument("--output", required=True)
    item.set_defaults(handler=vs_planning.create_vs_overlay)

    item = sub.add_parser("capture-vs-environment")
    item.add_argument("--vsdevcmd", required=True)
    item.add_argument("--arch", required=True)
    item.add_argument("--host-arch", required=True)
    item.add_argument("--include-env", action="append", default=[])
    item.add_argument("--timeout", type=int, default=60)
    item.add_argument("--output", required=True)
    item.set_defaults(handler=vs_planning.capture_vs_environment)

    item = sub.add_parser("plan-vs")
    item.add_argument("--project", required=True)
    item.add_argument("--project-root")
    item.add_argument("--msbuild", required=True)
    item.add_argument("--configuration", required=True)
    item.add_argument("--target", action="append", default=[])
    item.add_argument("--platform", required=True)
    item.add_argument("--wrapper-dir", required=True)
    item.add_argument("--overlay", required=True)
    item.add_argument("--environment", required=True)
    item.add_argument("--vc-targets-path")
    item.add_argument("--artifact", action="append", default=[])
    item.add_argument("--required-wrapper", action="append", choices=["cl.exe", "link.exe", "lib.exe"], required=True)
    item.add_argument("--real-tool", action="append", default=[], metavar="TOOL=PATH")
    item.add_argument("--timeout", type=int, default=3600)
    item.add_argument("--parallel", type=int, default=processes.default_parallelism())
    item.add_argument("--output")
    item.set_defaults(handler=vs_planning.plan_vs)

    item = sub.add_parser("plan-cmake")
    item.add_argument("--source", required=True)
    item.add_argument("--preserve-source-spelling", action="store_true")
    item.add_argument("--project-root")
    item.add_argument("--build", required=True)
    item.add_argument("--cmake", required=True)
    item.add_argument("--generator")
    item.add_argument("--configuration")
    item.add_argument("--c-compiler")
    item.add_argument("--cxx-compiler")
    item.add_argument("--ar")
    item.add_argument("--linker")
    item.add_argument("--cover-config", default="gcc.config")
    item.add_argument("--glue-code-compiler")
    item.add_argument("--glue-compiler-provenance")
    item.add_argument("--configure-arg", action="append", default=[])
    item.add_argument("--require-role", action="append", choices=sorted(gcc_tools.GCC_ROLE_ENVIRONMENT), required=True)
    item.add_argument("--real-tool", action="append", default=[], metavar="ROLE=PATH")
    item.add_argument("--artifact", action="append", default=[])
    item.add_argument("--parallel", type=int, default=processes.default_parallelism())
    item.add_argument("--timeout", type=int, default=3600)
    item.add_argument("--output")
    item.set_defaults(handler=gcc_tools.plan_cmake)

    def add_native_gcc_plan_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--project-root")
        command.add_argument("--working-directory")
        command.add_argument("--c-compiler")
        command.add_argument("--cxx-compiler")
        command.add_argument("--ar")
        command.add_argument("--linker")
        command.add_argument("--cover-config", default="gcc.config")
        command.add_argument("--glue-code-compiler")
        command.add_argument("--glue-compiler-provenance")
        command.add_argument(
            "--require-role",
            action="append",
            choices=sorted(gcc_tools.GCC_ROLE_ENVIRONMENT),
            required=True,
            help="Wrapper roles proven necessary by the native command listing",
        )
        command.add_argument(
            "--real-tool",
            action="append",
            default=[],
            metavar="ROLE=PATH",
            help="Known real compiler/binutil path used to reject wrapper bypasses",
        )
        command.add_argument(
            "--allow-native-ar-fallback",
            action="store_true",
            help="Allow only the explicitly mapped native ar after a failed Windows SP8 wrapper probe",
        )
        command.add_argument("--target", action="append", default=[])
        command.add_argument("--artifact", action="append", default=[])
        command.add_argument("--parallel", type=int, default=processes.default_parallelism())
        command.add_argument("--timeout", type=int, default=3600)
        command.add_argument("--output")

    item = sub.add_parser("plan-make")
    item.add_argument("--make", required=True)
    item.add_argument("--makefile", required=True)
    item.add_argument("--clean-target", default="clean")
    item.add_argument("--force-rebuild", action="store_true")
    add_native_gcc_plan_arguments(item)
    item.set_defaults(handler=gcc_tools.plan_make)

    item = sub.add_parser("verify-cmake-cache")
    item.add_argument("--plan", required=True)
    item.add_argument("--cache", required=True)
    item.add_argument("--output")
    item.set_defaults(handler=gcc_tools.verify_cmake_cache)

    item = sub.add_parser("execute-plan")
    item.add_argument("--plan", required=True)
    item.add_argument("--phase", required=True, choices=["preflight", "configure", "clean", "build", "rebuild"])
    item.add_argument("--log-dir", required=True)
    item.add_argument("--output")
    item.set_defaults(handler=processes.execute_plan)

    item = sub.add_parser("snapshot-artifacts")
    item.add_argument("--artifact", action="append", required=True)
    item.add_argument("--output", required=True)
    item.set_defaults(handler=processes.snapshot_artifacts)

    item = sub.add_parser("verify-artifacts")
    item.add_argument("--before", required=True)
    item.add_argument("--after", required=True)
    item.add_argument("--output")
    item.set_defaults(handler=processes.verify_artifacts)

    item = sub.add_parser("verify-output-paths")
    item.add_argument("--original-artifact", action="append", required=True)
    item.add_argument("--coverage-artifact", action="append", required=True)
    item.add_argument("--output")
    item.set_defaults(handler=pathutil.verify_output_paths)

    item = sub.add_parser("api-call")
    item.add_argument("--contract", required=True)
    item.add_argument("--base-url", required=True)
    item.add_argument("--operation", required=True)
    item.add_argument("--payload")
    item.add_argument("--query", action="append", default=[])
    item.add_argument("--path-param", action="append", default=[], help="Path parameter KEY=VALUE, e.g. id=<ObjectId>")
    item.add_argument("--token-env", default="COVER_ACCESS_KEY")
    item.add_argument("--ca-file")
    item.add_argument("--allow-mutation", action="store_true")
    item.add_argument("--skip-contract-check", action="store_true")
    item.add_argument("--timeout", type=int, default=30)
    item.add_argument("--output")
    item.set_defaults(handler=open_api.api_call)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init-job" and args.job_context:
            raise errors.ExecutorError("INVALID_JOB_CONTEXT", "init-job cannot consume an existing job context")
        if args.job_context:
            CURRENT_JOB_ID = jobs.load_job_context(args.job_context)["jobId"]
        else:
            CURRENT_JOB_ID = None
        result = args.handler(args)
        artifact_output_commands = {
            "create-vs-overlay",
            "capture-vs-environment",
            "plan-vs",
            "plan-cmake",
            "plan-make",
            "snapshot-artifacts",
            "api-call",
        }
        envelopes.emit(result, None if args.command in artifact_output_commands else getattr(args, "output", None))
        return 0
    except errors.ExecutorError as exc:
        envelopes.emit(envelopes.envelope(args.command.upper().replace("-", "_"), "FAILED", error={"code": exc.code, "message": str(exc), "evidence": exc.evidence}))
        return 2
    except subprocess.TimeoutExpired as exc:
        envelopes.emit(envelopes.envelope(args.command.upper().replace("-", "_"), "FAILED", error={"code": "COMMAND_TIMEOUT", "message": str(exc), "evidence": []}))
        return 3
    except (OSError, ValueError, json.JSONDecodeError, ET.ParseError) as exc:
        envelopes.emit(envelopes.envelope(args.command.upper().replace("-", "_"), "FAILED", error={"code": "EXECUTOR_INPUT_OR_IO_ERROR", "message": str(exc), "evidence": []}))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
