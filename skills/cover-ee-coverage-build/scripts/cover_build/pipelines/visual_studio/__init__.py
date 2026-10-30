"""End-to-end coverage workflow for this build environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from cover_build.pipelines.visual_studio import tools as visual_studio
from cover_build.core.errors import WorkflowWaiting
from cover_build.core import state_store
from cover_build.planning import build_plan, selection
from cover_build.pipelines.runtime import PipelineRuntime
from cover_build.services import agent_open_api as agent
from cover_build.pipelines.visual_studio import msbuild as msbuild_service
from cover_build.common import processes as native_common
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.common import toolchains
from cover_build.core import errors
from cover_build.pipelines.visual_studio import planning as vs_planning


def run(
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
    *,
    runtime: PipelineRuntime,
) -> dict[str, Any]:
    argument = runtime.argument
    fallback_source = runtime.fallback_source
    finalize_environment_survey_plan = runtime.finalize_environment_survey_plan
    job_project_input_fingerprint = runtime.job_project_input_fingerprint
    job_work_path = runtime.job_work_path
    record = runtime.record
    resolve_fallback_artifact = runtime.resolve_fallback_artifact
    write_json = runtime.write_json
    write_original_build_evidence = runtime.write_original_build_evidence

    parallel = parallel or processes.default_parallelism()
    build_evidence = build_evidence or build_plan.collect_build_evidence(job, discovery)
    raw_original_plan = choices.get("originalBuildPlan")
    planning_choices = dict(choices)
    if isinstance(raw_original_plan, dict) and isinstance(raw_original_plan.get("adapterEntry"), str):
        planning_choices["buildEntry"] = raw_original_plan["adapterEntry"]
    entry, preflight_projects, configuration, platform = selection.select_visual_studio(discovery, planning_choices)
    selected_references_path = job_work_path(job, "temporary", "selected-build-references.json")
    selected_references = build_plan.collect_selected_references(job, [entry, *preflight_projects])
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
    if raw_original_plan is None and build_plan.requires_model_original_plan(build_evidence, "MSBUILD"):
        raise WorkflowWaiting(
            "ORIGINAL_BUILD_PLAN_REQUIRED",
            "Visual Studio build scripts require model interpretation before the original build can run",
            {
                "originalBuildPlan": [],
                "originalBuildCommandSource": ["MODEL_INFERRED"],
            },
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
            evidence=[str(Path(job["directories"]["temporary"]) / "build-evidence.json"), str(selected_references_path)],
        )
    model_plan = (
        build_plan.validate_original_build_plan(job, raw_original_plan, "MSBUILD")
        if raw_original_plan is not None
        else None
    )
    if model_plan is not None:
        declared_source = fallback_source(choices)
        if declared_source != model_plan["origin"]:
            raise errors.ExecutorError(
                "INVALID_MODEL_BUILD_COMMAND",
                "originalBuildCommandSource must match originalBuildPlan.origin",
                [declared_source, model_plan["origin"]],
            )
        if not pathutil.same_path(model_plan["adapterEntry"], entry):
            raise errors.ExecutorError(
                "INVALID_ORIGINAL_BUILD_PLAN",
                "The MSBuild original plan adapterEntry conflicts with project selection",
                [model_plan["adapterEntry"], entry],
            )
    original_configuration, original_platform = selection.select_original_visual_studio_configuration(
        discovery,
        entry,
        preflight_projects,
        configuration,
        platform,
        choices,
    )
    original_project_configurations = selection.resolve_visual_studio_project_configurations(
        discovery,
        entry,
        preflight_projects,
        original_configuration,
        original_platform,
    )
    coverage_project_configurations = selection.resolve_visual_studio_project_configurations(
        discovery,
        entry,
        preflight_projects,
        configuration,
        platform,
    )
    selected_msbuild = choices.get("msbuildPath")
    if model_plan is not None:
        if selected_msbuild is not None and not pathutil.same_path(selected_msbuild, model_plan["buildTool"]):
            raise errors.ExecutorError(
                "MSBUILD_SELECTION_CONFLICT",
                "msbuildPath conflicts with originalBuildPlan.buildTool",
                [str(selected_msbuild), model_plan["buildTool"]],
            )
        selected_msbuild = model_plan["buildTool"]
    msbuild, vsdevcmd = visual_studio.find_tools(
        selected_msbuild,
        choices.get("vsDevCmdPath"),
        visual_studio.project_platform_toolsets(preflight_projects),
    )
    env_path = Path(job["directories"]["temporary"]) / "vs-environment.json"
    environment = record(job, stages, 5, "capture-vs-environment", vs_planning.capture_vs_environment(argument(vsdevcmd=str(vsdevcmd), arch=original_platform.lower() if original_platform.lower() in {"x86", "x64", "arm", "arm64"} else "x64", host_arch="x64", include_env=[], timeout=60, output=str(env_path))))
    overlay = Path(job["directories"]["temporary"]) / "cover-msbuild-overlay.targets"
    record(job, stages, 5, "create-vs-overlay", vs_planning.create_vs_overlay(argument(output=str(overlay))))
    original_plan = model_plan or msbuild_service.create_msbuild_original_plan(
        job,
        msbuild,
        Path(entry),
        original_configuration,
        original_platform,
        environment["data"]["environment"],
        environment_initialization={"method": "VSDEVCMD_CAPTURED", "scriptPath": str(vsdevcmd)},
        parallel=parallel,
    )
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
                "origin": original_plan["origin"],
                "evidenceSource": original_plan["evidenceSource"],
                "environmentInitialization": original_plan["environmentInitialization"],
                "driver": "MSBUILD",
                "plan": str(original_plan_path),
                "executable": original_plan["buildTool"],
                "entry": entry,
                "configuration": original_configuration,
                "platform": original_platform,
                "coverageConfiguration": configuration,
                "coveragePlatform": platform,
                "originalProjectConfigurations": original_project_configurations,
                "coverageProjectConfigurations": coverage_project_configurations,
                "commands": original_plan["commands"],
                "coverageInjection": original_plan["coverageInjection"],
                "evidence": original_plan["evidence"],
            },
        ),
    )
    finalize_environment_survey_plan(job, stages, original_plan)
    native_common.require_execution(execute)
    original_output_projects = [
        mapping
        for mapping in original_project_configurations
        if not visual_studio.is_utility_configuration(
            Path(mapping["project"]), mapping["configuration"], mapping["platform"]
        )
    ]
    if not original_output_projects:
        raise errors.ExecutorError(
            "NO_COVERAGE_ELIGIBLE_OUTPUT",
            "The selected Visual Studio configuration has no non-Utility project that can produce a coverage target",
            [str(selected_references_path)],
        )
    try:
        native_build_result = state_store.run_checkpointed(
            job,
            stages,
            record,
            5,
            "execute-original-build",
            {
                "plan": original_plan,
                "projectConfigurations": original_project_configurations,
                "toolOverrides": [(name, str(path)) for name, path in (real_tool_overrides or [])],
                "selectedReferencesFingerprint": build_plan.selected_references_fingerprint(selected_references),
                "projectInputFingerprint": job_project_input_fingerprint(job),
            },
            lambda: msbuild_service.execute_msbuild(
                job,
                msbuild,
                Path(entry),
                Path(original_output_projects[0]["project"]),
                original_configuration,
                original_platform,
                environment["data"]["environment"],
                real_tool_overrides or [],
                project_configuration=original_output_projects[0]["configuration"],
                project_platform=original_output_projects[0]["platform"],
                original_plan=original_plan,
                parallel=parallel,
                project_configurations=original_output_projects,
            ),
        )
    except WorkflowWaiting as exc:
        if exc.code != "ORIGINAL_TOOLCHAIN_AMBIGUOUS" or not entry.casefold().endswith(".sln"):
            raise
        raise WorkflowWaiting(
            exc.code,
            exc.message,
            {
                "buildEntry": [item["project"] for item in original_project_configurations],
                "originalToolPath": exc.choices.get("originalToolPath", {}),
            },
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=exc.evidence,
        ) from exc
    native_build = native_build_result
    write_original_build_evidence(job, stages, native_build)
    toolchain = native_build["data"]["toolchain"]
    record(
        job,
        stages,
        5,
        "extract-original-toolchain",
        envelopes.envelope("EXTRACTING_ORIGINAL_TOOLCHAIN", "SUCCEEDED", toolchain),
    )
    real_tools = {name: Path(path) for name, path in toolchain["tools"].items()}
    if ensure_authentication is not None:
        ensure_authentication()
    descriptor = agent.ensure_wrapper_mappings(
        job,
        stages,
        record,
        allow_start,
        required_wrappers=set(real_tools),
        real_tools_override=real_tools,
        cover_user_name=choices.get("coverUserName"),
        toolchain_selection=choices.get("toolChainInfoSelection"),
        original_environment=environment["data"]["environment"],
        allow_wrapper_aliases=False,
    )
    selected_mapping = descriptor.get("resolvedWrapperMappings")
    if selected_mapping:
        real_tools = {name: Path(path) for name, path in selected_mapping.items()}
    record(job, stages, 9, "verify-toolchain", toolchains.verify_toolchain(argument(agent_root=descriptor["agentRoot"], tool=[f"{name}={path}" for name, path in real_tools.items()])))
    artifacts: list[Path] = []
    supplied_artifacts = list(explicit_artifacts or [])
    if supplied_artifacts:
        for value in supplied_artifacts:
            resolved = resolve_fallback_artifact(job, value)
            if not any(pathutil.same_path(resolved, existing) for existing in artifacts):
                artifacts.append(resolved)
    else:
        solution_dir = Path(entry).parent if Path(entry).suffix.lower() == ".sln" else None
        for index, project_mapping in enumerate(coverage_project_configurations, start=1):
            if visual_studio.is_utility_configuration(
                Path(project_mapping["project"]), project_mapping["configuration"], project_mapping["platform"]
            ):
                continue
            artifact = visual_studio.read_target_path(
                msbuild,
                Path(project_mapping["project"]),
                project_mapping["configuration"],
                project_mapping["platform"],
                environment["data"]["environment"],
                Path(job["directories"]["logs"]) / f"target-path-{index}.log",
                solution_dir,
            )
            if not any(pathutil.same_path(artifact, existing) for existing in artifacts):
                artifacts.append(artifact)
    if not artifacts:
        raise WorkflowWaiting(
            "TARGET_OUTPUT_AMBIGUOUS",
            "No verifiable executable or library output was resolved for the selected Visual Studio build",
            {"expectedArtifacts": []},
            decision_owner="USER",
            purpose="BUILD_OUTPUT",
            evidence=[str(selected_references_path)],
    )
    plan_path = Path(job["directories"]["temporary"]) / "build-plan.json"
    plan = record(job, stages, 11, "plan-vs", vs_planning.plan_vs(argument(project=entry, project_root=job["projectRoot"], msbuild=str(msbuild), configuration=configuration, platform=platform, wrapper_dir=descriptor["toolchainProvisioning"]["wrapperDirectory"], overlay=str(overlay), environment=str(env_path), vc_targets_path=None, artifact=[str(artifact) for artifact in artifacts], required_wrapper=sorted(real_tools), real_tool=[f"{name}={path}" for name, path in real_tools.items()], timeout=3600, parallel=parallel, output=str(plan_path))))
    build_result = msbuild_service.execute_msbuild_plan(job, stages, record, plan_path, artifacts, execute)
    coverage_plan = fileio.load_json(plan_path)

    def verify_applied() -> dict[str, Any]:
        return msbuild_service.verify_applied_msbuild(job, coverage_plan, artifacts)

    return {
        **build_result,
        "coverageVerified": True,
        "_manualCoverage": {
            "buildSystem": "VISUAL_STUDIO",
            "commands": [coverage_plan["commands"]["rebuild"]],
            "assets": [str(overlay)],
            "artifacts": [str(artifact) for artifact in artifacts],
            "verifyApplied": verify_applied,
            "mode": "PROJECT_LOCAL_MSBUILD_LAUNCHER",
            "details": {
                "entry": entry,
                "configuration": configuration,
                "platform": platform,
                "projects": [item["project"] for item in coverage_project_configurations],
                "projectConfigurations": coverage_project_configurations,
                "wrapperDirectory": descriptor["toolchainProvisioning"]["wrapperDirectory"],
            },
        },
    }
