#!/usr/bin/env python3
"""Run the deterministic portion of a COVER coverage-build workflow.

The runner owns stage ordering.  It writes a resumable state record and assigns
each unresolved decision to either the model or the user.  Build-environment
ambiguity is model-owned; user input is reserved for explicit permissions,
missing customer configuration, and the final original-command fallback.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


from cover_build.pipelines import registry as pipeline_registry
from cover_build.common.jobs import argument
from cover_build.common import open_api_credentials
from cover_build.common.pathutil import canonical
from cover_build.common.fileio import write_json
from cover_build.core.job_evidence import (
    WORKFLOW_SCHEMA,
    finalize_environment_survey_plan,
    job_project_input_fingerprint,
    job_work_path,
    record,
    record_coverage_injection_evidence,
    write_original_build_evidence,
)
from cover_build.core.errors import WorkflowWaiting
from cover_build import workflow
from cover_build.planning import build_plan, selection
from cover_build.planning.resolution import (
    DECISION_HISTORY_SCHEMA,
    apply_model_fallback,
    fallback_source,
    load_decision,
    load_decision_history,
    load_resumable_decision,
    resolve_fallback_artifact,
    waiting,
)
from cover_build.pipelines.runtime import PipelineRuntime
from cover_build.services import agent_open_api as agent
from cover_build.services import gcc_coverage
from cover_build.pipelines.gcc import original as gcc_original
from cover_build.pipelines.visual_studio import msbuild
from cover_build.core import errors


DEFAULT_GCC_COVER_CONFIG = "gcc.config"

def selected_cover_config(args: argparse.Namespace, build_system: str) -> str:
    """Use gcc.config for STM32CubeIDE; otherwise allow an explicit override."""
    if build_system == "STM32CUBEIDE":
        return DEFAULT_GCC_COVER_CONFIG
    requested = getattr(args, "gcc_cover_config", None)
    if requested:
        return str(requested)
    return DEFAULT_GCC_COVER_CONFIG


def select_build_system(
    supported_build_systems: list[str],
    stm32cubeide_discovery: dict[str, Any],
    choices: dict[str, Any],
    evidence_path: Path | None = None,
) -> str:
    """Prefer a confirmed STM32CubeIDE managed project over its generated Makefiles."""
    status = stm32cubeide_discovery.get("status")
    evidence = [str(evidence_path)] if evidence_path is not None else []
    if status == "CONFIRMED":
        supplied = choices.get("buildSystem")
        if supplied not in {None, "STM32CUBEIDE"}:
            raise errors.ExecutorError(
                "INVALID_WORKFLOW_DECISION",
                "The selected build system conflicts with a confirmed STM32CubeIDE managed project",
                stm32cubeide_discovery.get("projects") or evidence,
            )
        return "STM32CUBEIDE"
    if status == "CANDIDATE":
        candidates = list(dict.fromkeys(["STM32CUBEIDE", *supported_build_systems]))
        supplied = choices.get("buildSystem")
        if supplied is not None:
            return selection.select_one("buildSystem", candidates, choices)
        raise WorkflowWaiting(
            "STM32CUBEIDE_CLASSIFICATION_REQUIRED",
            "Eclipse CDT markers were found, but they do not prove that this is an STM32CubeIDE managed project",
            {"buildSystem": candidates},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=evidence or list(stm32cubeide_discovery.get("candidateEvidence") or []),
        )
    if status == "NOT_DETECTED":
        return selection.select_one("buildSystem", supported_build_systems, choices)
    raise errors.ExecutorError(
        "INVALID_STM32CUBEIDE_DISCOVERY",
        f"Unsupported STM32CubeIDE discovery status: {status}",
        [stm32cubeide_discovery.get("schema")],
    )


def probe_windows_gcc_ar_wrapper(wrapper: str, agent_root: str, cover_config: str) -> dict[str, Any]:
    if os.name != "nt" or not Path(wrapper).is_file():
        return {"applicable": False, "fallbackRequired": False}
    env = os.environ.copy()
    env["CODESCROLL_TM_INSTALL_PATH"] = agent_root
    env["COVER_BR_CONFIG"] = cover_config
    result = subprocess.run([wrapper, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=20, env=env, check=False)
    return {"applicable": True, "fallbackRequired": result.returncode != 0, "exitCode": result.returncode}

def prompt_credentials(require_public: bool) -> None:
    """Populate only this runner process; never serialize user-entered secrets."""
    if not require_public or os.environ.get("COVER_ACCESS_KEY"):
        return
    cached = open_api_credentials.resolve_access_key()
    os.environ["COVER_ACCESS_KEY"] = cached if cached else getpass.getpass("COVER Access Key: ")


def _pipeline_runtime() -> PipelineRuntime:
    return PipelineRuntime(
        argument=argument,
        fallback_source=fallback_source,
        finalize_environment_survey_plan=finalize_environment_survey_plan,
        job_project_input_fingerprint=job_project_input_fingerprint,
        job_work_path=job_work_path,
        probe_windows_gcc_ar_wrapper=probe_windows_gcc_ar_wrapper,
        record=record,
        record_coverage_injection_evidence=record_coverage_injection_evidence,
        resolve_fallback_artifact=resolve_fallback_artifact,
        selected_cover_config=selected_cover_config,
        write_json=write_json,
        write_original_build_evidence=write_original_build_evidence,
    )


def run_visual_studio(
    job: dict[str, Any],
    discovery: dict[str, Any],
    choices: dict[str, Any],
    stages: list[dict[str, Any]],
    execute: bool,
    allow_start: bool,
    real_tool_overrides: list[tuple[str, Path]] | None = None,
    build_evidence: dict[str, Any] | None = None,
    explicit_artifacts: list[str] | None = None,
    parallel: int | None = None,
    ensure_authentication: Any | None = None,
) -> dict[str, Any]:
    return pipeline_registry.load_pipeline("VISUAL_STUDIO").run(
        job, discovery, choices, stages, execute, allow_start,
        real_tool_overrides, build_evidence, explicit_artifacts, parallel,
        ensure_authentication, runtime=_pipeline_runtime(),
    )





def run_stm32cubeide(
    job: dict[str, Any],
    discovery: dict[str, Any],
    stm32_discovery: dict[str, Any],
    choices: dict[str, Any],
    stages: list[dict[str, Any]],
    args: argparse.Namespace,
    ensure_authentication: Any | None = None,
) -> dict[str, Any]:
    return pipeline_registry.load_pipeline("STM32CUBEIDE").run(
        job, discovery, stm32_discovery, choices, stages, args,
        ensure_authentication, runtime=_pipeline_runtime(),
    )



def run_gcc(
    job: dict[str, Any],
    discovery: dict[str, Any],
    build_system: str,
    choices: dict[str, Any],
    stages: list[dict[str, Any]],
    args: argparse.Namespace,
    build_evidence: dict[str, Any] | None = None,
    ensure_authentication: Any | None = None,
) -> dict[str, Any]:
    return pipeline_registry.load_pipeline(build_system).run(
        job, discovery, build_system, choices, stages, args,
        build_evidence, ensure_authentication, runtime=_pipeline_runtime(),
    )



def _workflow_runtime() -> workflow.WorkflowRuntime:
    return workflow.WorkflowRuntime(
        prompt_credentials=prompt_credentials,
        run_gcc=run_gcc,
        run_stm32cubeide=run_stm32cubeide,
        run_visual_studio=run_visual_studio,
        select_build_system=select_build_system,
    )


def main(argv: list[str] | None = None) -> int:
    return workflow.run(argv, runtime=_workflow_runtime())


if __name__ == "__main__":
    sys.exit(main())
