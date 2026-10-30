"""End-to-end coverage workflow for this build environment."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from typing import Any


from cover_build.pipelines.gcc import selection as gcc
from cover_build.pipelines.stm32cubeide import tools as stm32cubeide
from cover_build.core import state_store
from cover_build.core.errors import WorkflowWaiting
from cover_build.planning import build_plan
from cover_build.pipelines.runtime import PipelineRuntime
from cover_build.services import agent_open_api as agent
from cover_build.services import gcc_coverage
from cover_build.services import glue_compiler
from cover_build.common import processes as native_common
from cover_build.services import project_backup
from cover_build.common import envelopes
from cover_build.common import pathutil
from cover_build.common import toolchains
from cover_build.core import errors


def _workspace_outside_project(candidate: Path, project_directory: Path, category: str, identity: str) -> Path:
    candidate = candidate.resolve()
    project_directory = project_directory.resolve()
    if not pathutil.path_within(candidate, project_directory):
        return candidate
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return project_directory.parent / category / "stm32cubeide-workspaces" / key


def _job_workspace(job: dict[str, Any], project: dict[str, Any]) -> Path:
    temporary = Path(job["directories"]["temporary"])
    identity = str(job.get("jobId") or temporary.parent.name)
    return _workspace_outside_project(
        temporary / "stm32cubeide-workspace",
        Path(project["projectDirectory"]),
        ".cover-build",
        identity,
    )


def _persistent_workspace(job: dict[str, Any], project: dict[str, Any]) -> Path:
    project_directory = Path(project["projectDirectory"])
    return _workspace_outside_project(
        Path(job["projectRoot"]) / ".cover-ee" / "stm32cubeide-workspace",
        project_directory,
        ".cover-ee",
        str(project_directory.resolve()).casefold(),
    )


def run(
    job: dict[str, Any],
    discovery: dict[str, Any],
    stm32_discovery: dict[str, Any],
    choices: dict[str, Any],
    stages: list[dict[str, Any]],
    args: argparse.Namespace,
    ensure_authentication: Any | None = None,
    *,
    runtime: PipelineRuntime,
) -> dict[str, Any]:
    argument = runtime.argument
    finalize_environment_survey_plan = runtime.finalize_environment_survey_plan
    job_project_input_fingerprint = runtime.job_project_input_fingerprint
    job_work_path = runtime.job_work_path
    record = runtime.record
    resolve_fallback_artifact = runtime.resolve_fallback_artifact
    selected_cover_config = runtime.selected_cover_config
    write_json = runtime.write_json
    write_original_build_evidence = runtime.write_original_build_evidence

    project = stm32cubeide.select_project(stm32_discovery, choices)
    executable = stm32cubeide.find_headless_executable(
        getattr(args, "stm32cubeide_executable", None) or choices.get("stm32CubeIDEExecutable")
    )
    workspace = _job_workspace(job, project)
    workspace.mkdir(parents=True, exist_ok=True)
    selected_references = build_plan.collect_selected_references(
        job, [project["projectFile"], project["cprojectFile"], *project.get("iocFiles", [])]
    )
    selected_references_path = job_work_path(job, "temporary", "selected-build-references.json")
    write_json(selected_references_path, selected_references)
    record(
        job,
        stages,
        3,
        "collect-selected-build-references",
        envelopes.envelope(
            "COLLECTING_SELECTED_BUILD_REFERENCES",
            "SUCCEEDED",
            {"path": str(selected_references_path), "fileCount": len(selected_references["files"]), "unresolvedCount": len(selected_references["unresolved"])},
        ),
    )
    original_plan = stm32cubeide.create_original_plan(job, executable, workspace, project)
    original_plan_path = Path(job["directories"]["temporary"]) / "original-build-plan.json"
    write_json(original_plan_path, original_plan)
    record(
        job,
        stages,
        4,
        "resolve-original-build-command",
        envelopes.envelope(
            "RESOLVING_ORIGINAL_BUILD_COMMAND",
            "SUCCEEDED",
            {
                "tier": original_plan["origin"],
                "evidenceSource": original_plan["evidenceSource"],
                "driver": "STM32CUBEIDE",
                "plan": str(original_plan_path),
                "commands": original_plan["commands"],
                "coverageInjection": original_plan["coverageInjection"],
                "project": project["projectName"],
                "configuration": project["selectedConfiguration"],
            },
        ),
    )
    finalize_environment_survey_plan(job, stages, original_plan)
    native_common.require_execution(args.execute)
    native_build = state_store.run_checkpointed(
        job,
        stages,
        record,
        5,
        "execute-original-build",
        {
            "plan": original_plan,
            "selectedReferencesFingerprint": build_plan.selected_references_fingerprint(selected_references),
            "projectInputFingerprint": job_project_input_fingerprint(job),
        },
        lambda: stm32cubeide.execute_original(job, original_plan),
    )
    write_original_build_evidence(job, stages, native_build)
    toolchain = native_build["data"]["toolchain"]
    record(
        job,
        stages,
        5,
        "extract-original-toolchain",
        envelopes.envelope("EXTRACTING_ORIGINAL_TOOLCHAIN", "SUCCEEDED", toolchain),
    )
    artifacts = list(getattr(args, "artifact", []))
    if not artifacts:
        artifacts = [item["path"] for item in native_build["data"].get("artifacts", [])]
    artifacts = [str(resolve_fallback_artifact(job, value)) for value in artifacts]
    if not artifacts:
        raise WorkflowWaiting(
            "EXPECTED_ARTIFACT_REQUIRED",
            "STM32CubeIDE original build succeeded but no ELF/AXF/OUT artifact path was resolved",
            {"expectedArtifacts": []},
            decision_owner="MODEL",
            purpose="BUILD_OUTPUT",
            evidence=[native_build["data"]["commands"]["build"]["log"]],
        )
    native_tool_directory = stm32cubeide.native_tool_directory(executable)
    wrapper_mapping, original_prefix = gcc_coverage.gcc_wrapper_mapping(
        toolchain,
        search_directories=[native_tool_directory],
    )
    if ensure_authentication is not None:
        ensure_authentication()
    original_environment = dict(os.environ)
    original_environment.update(original_plan["commands"]["build"].get("environmentDelta") or {})
    original_environment["PATH"] = os.pathsep.join([str(native_tool_directory), original_environment.get("PATH", "")])
    descriptor = agent.ensure_wrapper_mappings(
        job,
        stages,
        record,
        args.allow_agent_start,
        required_wrappers=set(wrapper_mapping),
        real_tools_override=wrapper_mapping,
        capability="STM32CUBEIDE",
        cover_user_name=choices.get("coverUserName"),
        original_environment=original_environment,
        allow_wrapper_aliases=False,
    )
    if descriptor.get("resolvedWrapperMappings"):
        wrapper_mapping = {name: Path(path) for name, path in descriptor["resolvedWrapperMappings"].items()}
    selected_wrappers = gcc.select_wrapper_set(
        descriptor,
        choices,
        expected_prefix=original_prefix,
        toolchain=toolchain,
        wrapper_mapping=wrapper_mapping,
    )
    glue_runtime = glue_compiler.prepare_runtime(
        job,
        toolchain,
        descriptor["agentRoot"],
        selected_cover_config(args, "STM32CUBEIDE"),
        search_directories=[native_tool_directory],
        selected_compiler=choices.get("glueCodeCompiler"),
        remove_stm32_incompatible_link_flags=True,
    )
    record(
        job,
        stages,
        9,
        "resolve-glue-code-compiler",
        envelopes.envelope("RESOLVING_GLUE_CODE_COMPILER", "SUCCEEDED", glue_runtime),
    )
    real_tools = {role: str(path) for role, path in (toolchain.get("invokedTools") or toolchain["tools"]).items()}
    wrappers = {
        role: selected_wrappers[role]
        for role in real_tools
        if role in selected_wrappers and role in {"c_compiler", "cxx_compiler", "linker", "ar"}
    }
    record(
        job,
        stages,
        9,
        "verify-toolchain",
        toolchains.verify_toolchain(
            argument(agent_root=descriptor["agentRoot"], tool=[f"{name}={path}" for name, path in wrapper_mapping.items()])
        ),
    )
    cproject = Path(project["cprojectFile"])
    absolute_commands = project_backup.stm32cubeide_absolute_tool_commands(
        cproject,
        project["selectedConfiguration"],
    )
    patchable_commands = [item for item in absolute_commands if item["role"] in wrappers]
    patch: project_backup.ProjectFilePatch | None = None
    if patchable_commands:
        patch = project_backup.ProjectFilePatch.create(job, "STM32CUBEIDE_CPROJECT", [cproject])
        patch.backup()
        overrides = patch.apply_stm32cubeide(project["selectedConfiguration"], wrappers)
        try:
            record(
                job,
                stages,
                10,
                "patch-stm32cubeide-tool-commands",
                envelopes.envelope(
                    "PATCHING_STM32CUBEIDE_TOOL_COMMANDS",
                    "SUCCEEDED",
                    {"strategy": "TEMPORARY_CPROJECT_TOOL_COMMAND_OVERRIDE", "overrides": overrides, "manifest": str(patch.manifest_path)},
                ),
            )
        except BaseException:
            patch.restore()
            raise
    else:
        record(
            job,
            stages,
            10,
            "patch-stm32cubeide-tool-commands",
            envelopes.envelope(
                "PATCHING_STM32CUBEIDE_TOOL_COMMANDS",
                "SKIPPED",
                {"strategy": "HEADLESS_PATH_PREPEND", "absoluteCommands": absolute_commands},
            ),
        )
    coverage_succeeded = False
    try:
        coverage_result = stm32cubeide.execute_coverage(
            job,
            executable,
            workspace,
            project,
            Path(descriptor["toolchainProvisioning"]["wrapperDirectory"]),
            wrappers,
            real_tools,
            artifacts,
            Path(descriptor["agentRoot"]),
            glue_runtime,
        )
        record(
            job,
            stages,
            11,
            "stm32cubeide-headless-coverage-build",
            envelopes.envelope(
                "EXECUTING_STM32CUBEIDE_COVERAGE_BUILD",
                "SUCCEEDED",
                {
                    "results": [coverage_result["command"]],
                    "wrapperVerification": coverage_result["wrapperVerification"],
                    "artifactVerification": coverage_result["artifactVerification"]["data"],
                    "glueCodeCompiler": coverage_result["glueCodeCompiler"],
                },
            ),
        )
        coverage_succeeded = True
    finally:
        if patch is not None:
            if coverage_succeeded:
                record(
                    job,
                    stages,
                    12,
                    "retain-stm32cubeide-cproject",
                    envelopes.envelope(
                        "RETAINING_STM32CUBEIDE_CPROJECT",
                        "SUCCEEDED",
                        {"manifest": str(patch.manifest_path), "policy": "PERSIST_AFTER_SUCCESS"},
                    ),
                )
            else:
                restore = patch.restore()
                record(
                    job,
                    stages,
                    12,
                    "restore-stm32cubeide-cproject",
                    envelopes.envelope("RESTORING_STM32CUBEIDE_CPROJECT", "SUCCEEDED", restore),
                )
    if patch is not None:
        coverage_result["projectFilePatch"] = {
            "manifest": str(patch.manifest_path),
            "restored": False,
        }
    agent_default_config = Path(descriptor["agentRoot"]) / "conf" / "gcc.config"
    persistent_workspace = _persistent_workspace(job, project)
    persistent_arguments = list(coverage_result["commandSpec"]["arguments"])
    try:
        data_index = persistent_arguments.index("-data")
        clean_index = persistent_arguments.index("-cleanBuild")
        if data_index + 1 >= len(persistent_arguments):
            raise ValueError("-data has no workspace argument")
    except ValueError as error:
        raise errors.ExecutorError(
            "PERSISTENT_STM32_COMMAND_INVALID",
            "The verified STM32CubeIDE command cannot be converted to a persistent workspace command",
            [str(error)],
        ) from error
    persistent_arguments[data_index + 1] = str(persistent_workspace)
    persistent_arguments[clean_index:clean_index] = ["-import", project["projectDirectory"]]
    persistent_command = {
        **coverage_result["commandSpec"],
        "arguments": persistent_arguments,
        "environmentDelta": {
            **dict(coverage_result["commandSpec"].get("environmentDelta") or {}),
            "COVER_BR_CONFIG": agent_default_config.name,
        },
    }

    def verify_applied_ide_environment() -> dict[str, Any]:
        return stm32cubeide.verify_persistent_ide_build(
            job,
            executable,
            project,
            Path(descriptor["toolchainProvisioning"]["wrapperDirectory"]),
            wrappers,
            real_tools,
            artifacts,
            Path(descriptor["agentRoot"]),
        )

    return {
        **coverage_result,
        "coverageVerified": True,
        "_manualCoverage": {
            "buildSystem": "STM32CUBEIDE",
            "commands": [persistent_command],
            "assets": [coverage_result["persistentAgentConfig"]],
            "artifacts": [item["path"] for item in coverage_result["artifacts"]],
            "mode": "STM32CUBEIDE_PROJECT_CONFIGURATION",
            "verifyApplied": verify_applied_ide_environment,
            "details": {
                "project": project["projectName"],
                "configuration": project["selectedConfiguration"],
                "projectRoot": job["projectRoot"],
                "cprojectFile": str(cproject),
                "wrapperDirectory": descriptor["toolchainProvisioning"]["wrapperDirectory"],
                "coverConfigName": agent_default_config.name,
                "ideBasePath": stm32cubeide.persistent_ide_base_path(executable),
                "persistentAgentConfig": str(agent_default_config),
                "stagedAgentConfig": coverage_result["persistentAgentConfig"],
                "verifiedAgentConfigSha256": pathutil.sha256_file(
                    Path(coverage_result["persistentAgentConfig"])
                ),
                "agentDefaultConfig": str(agent_default_config),
                "persistentWorkspace": str(persistent_workspace),
                "absoluteToolCommandsRequireProjectIntegration": patch is not None,
                "existingProjectChanges": patch.records if patch is not None else [],
            },
        },
    }
