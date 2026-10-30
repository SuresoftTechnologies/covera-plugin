"""Original and coverage MSBuild execution."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


from cover_build.pipelines.visual_studio import tools as visual_studio
from cover_build.core.errors import WorkflowWaiting
from cover_build.planning import build_plan
from cover_build.common.processes import _run
from cover_build.common.processes import _without_output
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors
from cover_build.pipelines.visual_studio import planning as vs_planning


def create_msbuild_original_plan(
    job: dict[str, Any],
    msbuild: Path,
    entry: Path,
    configuration: str,
    platform: str,
    environment: dict[str, str],
    timeout: int = 3600,
    environment_initialization: dict[str, str] | None = None,
    parallel: int | None = None,
) -> dict[str, Any]:
    parallel = parallel or processes.default_parallelism()
    diagnostic_log = Path(job["directories"]["logs"]) / "original-msbuild-diagnostic.log"
    arguments = [
        str(entry),
        "/nologo",
        f"/p:Configuration={configuration}",
        f"/p:Platform={platform}",
        *vs_planning.msbuild_windows_sdk_properties(environment),
        f"/m:{parallel}",
        "/t:Rebuild",
        "/v:minimal",
        "/fl",
        f"/flp:LogFile={diagnostic_log};Verbosity=diagnostic",
    ]
    return build_plan.validate_original_build_plan(
        job,
        {
            "schema": build_plan.ORIGINAL_BUILD_PLAN_SCHEMA,
            "jobId": job["jobId"],
            "origin": "SCRIPT_EXTRACTED",
            "evidenceSource": "SCRIPT",
            "environmentInitialization": environment_initialization or {"method": "INHERITED_PROCESS"},
            "driver": "MSBUILD",
            "buildTool": str(msbuild),
            "adapterEntry": str(entry),
            "originalBuildDirectory": str(entry.parent),
            "configuration": configuration,
            "targets": [f"{configuration}|{platform}"],
            "requiredRoles": ["compiler", "linker", "archiver"],
            "commands": {"build": processes.command_spec(msbuild, arguments, entry.parent, timeout)},
            "coverageInjection": {"strategy": "MSBUILD_EXECUTABLE_PATH_OVERLAY"},
            "diagnosticLog": str(diagnostic_log),
            "cleanBuild": True,
            "evidence": [str(entry)],
        },
        "MSBUILD",
    )

def execute_msbuild(
    job: dict[str, Any],
    msbuild: Path,
    entry: Path,
    project: Path,
    configuration: str,
    platform: str,
    environment: dict[str, str],
    overrides: list[tuple[str, Path]],
    project_configuration: str | None = None,
    project_platform: str | None = None,
    timeout: int = 3600,
    original_plan: dict[str, Any] | None = None,
    parallel: int | None = None,
    project_configurations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a native MSBuild Rebuild and extract only command-observed tools."""
    logs = Path(job["directories"]["logs"])
    temporary = Path(job["directories"]["temporary"])
    solution_dir = entry.parent if entry.suffix.casefold() == ".sln" else None
    selected_projects = project_configurations or [{
        "project": str(project),
        "configuration": project_configuration or configuration,
        "platform": project_platform or platform,
    }]
    artifacts: list[Path] = []
    for index, selected in enumerate(selected_projects, start=1):
        artifact = visual_studio.read_target_path(
            msbuild,
            Path(selected["project"]),
            str(selected["configuration"]),
            str(selected["platform"]),
            environment,
            logs / f"original-target-path-{index}.log",
            solution_dir,
        )
        if not any(pathutil.same_path(artifact, current) for current in artifacts):
            artifacts.append(artifact)
    before_path = temporary / "original-artifact-before.json"
    after_path = temporary / "original-artifact-after.json"
    before = processes.snapshot_artifacts(
        SimpleNamespace(artifact=[str(artifact) for artifact in artifacts], output=str(before_path))
    )

    plan = build_plan.validate_original_build_plan(
        job,
        original_plan or create_msbuild_original_plan(
            job,
            msbuild,
            entry,
            configuration,
            platform,
            environment,
            timeout,
            parallel=parallel,
        ),
        "MSBUILD",
    )
    diagnostic_log = Path(plan["diagnosticLog"])
    previous_diagnostic = None
    if diagnostic_log.is_file():
        previous_stat = diagnostic_log.stat()
        previous_diagnostic = {
            "size": previous_stat.st_size,
            "modifiedNs": previous_stat.st_mtime_ns,
            "sha256": pathutil.sha256_file(diagnostic_log),
        }
    commands: dict[str, dict[str, Any]] = {}
    for phase in ("clean", "build"):
        if phase not in plan["commands"]:
            continue
        spec = dict(plan["commands"][phase])
        if plan["origin"] == "SCRIPT_EXTRACTED" and pathutil.same_path(spec["executable"], msbuild):
            spec["environmentMode"] = "replace"
            spec["environment"] = environment
        commands[phase] = _run(
            spec,
            logs / f"original-msbuild-{phase}.log",
            "ORIGINAL_BUILD_FAILED",
            f"The original MSBuild {phase} command failed",
        )
    if not diagnostic_log.is_file():
        raise errors.ExecutorError(
            "ORIGINAL_BUILD_DIAGNOSTIC_MISSING",
            "MSBuild succeeded but did not create the requested diagnostic log",
            [str(diagnostic_log)],
        )
    current_stat = diagnostic_log.stat()
    current_diagnostic = {
        "size": current_stat.st_size,
        "modifiedNs": current_stat.st_mtime_ns,
        "sha256": pathutil.sha256_file(diagnostic_log),
    }
    if previous_diagnostic == current_diagnostic:
        raise errors.ExecutorError(
            "ORIGINAL_BUILD_DIAGNOSTIC_STALE",
            "The MSBuild diagnostic log was not newly created or changed by this original build",
            [str(diagnostic_log)],
        )

    after = processes.snapshot_artifacts(
        SimpleNamespace(artifact=[str(artifact) for artifact in artifacts], output=str(after_path))
    )
    artifact_verification = processes.verify_artifacts(
        SimpleNamespace(before=str(before_path), after=str(after_path), output=None)
    )
    observed = visual_studio.extract_toolchain_from_diagnostic(
        diagnostic_log,
        artifacts,
        working_directories=[entry.parent, *[artifact.parent for artifact in artifacts]],
    )
    resolved = visual_studio.apply_tool_overrides(observed, overrides)
    visual_studio.reject_cover_wrappers(resolved["tools"])
    return envelopes.envelope(
        "EXECUTING_ORIGINAL_BUILD",
        "SUCCEEDED",
        {
            "driver": "MSBUILD",
            "configuration": configuration,
            "platform": platform,
            "entry": str(entry),
            "artifact": str(artifacts[0]),
            "artifacts": [{"path": str(artifact)} for artifact in artifacts],
            "command": _without_output(commands["build"]),
            "commands": {name: _without_output(result) for name, result in commands.items()},
            "originalBuildPlan": plan,
            "diagnosticLog": str(diagnostic_log),
            "diagnosticLogEvidence": current_diagnostic,
            "artifactVerification": artifact_verification,
            "toolchain": resolved,
        },
    )

def execute_msbuild_plan(
    job: dict[str, Any],
    stages: list[dict[str, Any]],
    record: Callable[..., dict[str, Any]],
    plan_path: Path,
    artifacts: list[Path] | Path,
    execute: bool,
) -> dict[str, Any]:
    if not execute:
        raise WorkflowWaiting(
            "EXECUTION_NOT_ENABLED",
            "Plan is ready; build execution was not enabled",
            {"execute": True},
            decision_owner="USER",
            purpose="MUTATION_PERMISSION",
        )
    before_path = Path(job["directories"]["temporary"]) / "artifacts-before.json"
    after_path = Path(job["directories"]["temporary"]) / "artifacts-after.json"
    artifact_paths = [artifacts] if isinstance(artifacts, Path) else list(artifacts)
    if not artifact_paths:
        raise WorkflowWaiting(
            "EXPECTED_ARTIFACT_REQUIRED",
            "At least one MSBuild output is required before coverage execution",
            {"expectedArtifacts": []},
            decision_owner="MODEL",
            purpose="BUILD_OUTPUT",
        )
    coverage_plan = fileio.load_json(plan_path)
    comparison = pathutil.require_same_output_paths(
        [str(path) for path in artifact_paths],
        list(coverage_plan.get("expectedArtifacts") or [str(path) for path in artifact_paths]),
    )
    serialized = [str(path) for path in artifact_paths]
    record(job, stages, 12, "snapshot-before", processes.snapshot_artifacts(SimpleNamespace(artifact=serialized, output=str(before_path))))
    build_result = record(job, stages, 13, "rebuild", processes.execute_plan(SimpleNamespace(plan=str(plan_path), phase="rebuild", log_dir=job["directories"]["logs"])))
    record(job, stages, 14, "snapshot-after", processes.snapshot_artifacts(SimpleNamespace(artifact=serialized, output=str(after_path))))
    artifact_result = processes.verify_artifacts(SimpleNamespace(before=str(before_path), after=str(after_path), output=None))
    artifact_result.setdefault("data", {})["outputPathComparison"] = comparison
    artifact_result = record(job, stages, 15, "verify-artifacts", artifact_result)
    command_results = build_result.get("data", {}).get("results", [])
    if not command_results:
        raise errors.ExecutorError("BUILD_INTERVAL_UNAVAILABLE", "The MSBuild stage emitted no command timing evidence")
    return {
        "artifactVerification": artifact_result,
        "lastStage": 15,
        "artifacts": serialized,
        "outputPathComparison": comparison,
        "buildInterval": {
            "startedAt": command_results[0]["startedAt"],
            "finishedAt": command_results[-1]["finishedAt"],
        },
    }


def create_applied_msbuild_plan(coverage_plan: dict[str, Any]) -> dict[str, Any]:
    """Create an IDE-equivalent Rebuild plan with no transient wrapper properties."""
    plan = copy.deepcopy(coverage_plan)
    command = dict(plan["commands"]["rebuild"])
    arguments = list(command.get("arguments") or [])
    transient_prefixes = (
        "/p:forceimportbeforecpptargets=",
        "/p:coverwrapperdirectory=",
    )
    removed_by_prefix = {
        prefix: [argument for argument in arguments if argument.casefold().startswith(prefix)]
        for prefix in transient_prefixes
    }
    if any(len(values) != 1 for values in removed_by_prefix.values()):
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_PLAN_INVALID",
            "The verified MSBuild plan must contain exactly one of each transient wrapper property",
            removed_by_prefix,
        )
    command["arguments"] = [
        argument
        for argument in arguments
        if not argument.casefold().startswith(transient_prefixes)
    ]
    wrapper_directory = Path(plan["wrapperDirectory"]).resolve(strict=True)

    def neutralize_environment(raw: dict[str, Any]) -> dict[str, Any]:
        environment = dict(raw)
        for key in list(environment):
            if key.casefold() in {"forceimportbeforecpptargets", "coverwrapperdirectory"}:
                environment.pop(key)
        path_key = next((key for key in environment if key.casefold() == "path"), None)
        if path_key is not None:
            path_parts = [part for part in environment[path_key].split(";") if part]
            environment[path_key] = ";".join(
                part
                for part in path_parts
                if Path(part.strip().strip('"')).resolve() != wrapper_directory
            )
        return environment

    if isinstance(command.get("environment"), dict):
        command["environment"] = neutralize_environment(command["environment"])
    if isinstance(command.get("environmentDelta"), dict):
        command["environmentDelta"] = neutralize_environment(command["environmentDelta"])
    plan["commands"] = {"rebuild": command}
    plan["persistenceVerification"] = {
        "strategy": "PROJECT_PROPERTY_SHEET",
        "removedGlobalProperties": ["ForceImportBeforeCppTargets", "CoverWrapperDirectory"],
        "wrapperDirectoryRemovedFromLaunchPath": True,
    }
    return fileio.add_integrity(plan)


def verify_applied_msbuild(
    job: dict[str, Any],
    coverage_plan: dict[str, Any],
    artifacts: list[Path],
) -> dict[str, Any]:
    """Rebuild through project imports and prove wrappers plus same-path outputs."""
    temporary = Path(job["directories"]["temporary"])
    logs = Path(job["directories"]["logs"])
    plan_path = temporary / "applied-build-plan.json"
    before_path = temporary / "applied-artifacts-before.json"
    after_path = temporary / "applied-artifacts-after.json"
    applied_plan = create_applied_msbuild_plan(coverage_plan)
    fileio.atomic_write_json(plan_path, applied_plan)
    serialized = [str(path) for path in artifacts]
    processes.snapshot_artifacts(SimpleNamespace(artifact=serialized, output=str(before_path)))
    build_result = processes.execute_plan(
        SimpleNamespace(plan=str(plan_path), phase="rebuild", log_dir=str(logs))
    )
    processes.snapshot_artifacts(SimpleNamespace(artifact=serialized, output=str(after_path)))
    artifact_result = processes.verify_artifacts(
        SimpleNamespace(before=str(before_path), after=str(after_path), output=None)
    )
    command_results = build_result.get("data", {}).get("results", [])
    wrapper_verifications = build_result.get("data", {}).get("verifications", [])
    if not command_results or not wrapper_verifications:
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_VERIFICATION_INCOMPLETE",
            "The project-linked Rebuild did not emit command and wrapper evidence",
        )
    required_wrappers = set(applied_plan.get("wrapperVerificationNames") or [])
    verified_wrappers = {
        name
        for verification in wrapper_verifications
        for name in (verification.get("verifiedWrappers") or {})
    }
    if not required_wrappers or verified_wrappers != required_wrappers:
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_WRAPPER_VERIFICATION_FAILED",
            "The project-linked Rebuild did not verify every required COVER wrapper",
            sorted(required_wrappers - verified_wrappers),
        )
    verified_artifacts = artifact_result.get("data", {}).get("artifacts", [])
    comparison = pathutil.require_same_output_paths(
        serialized,
        [str(item.get("path")) for item in verified_artifacts],
    )
    if len(verified_artifacts) != len(serialized) or any(
        item.get("exists") is not True or item.get("fresh") is not True
        for item in verified_artifacts
    ):
        raise errors.ExecutorError(
            "PERSISTENT_MSBUILD_ARTIFACT_VERIFICATION_FAILED",
            "The project-linked Rebuild did not refresh every same-path artifact",
            verified_artifacts,
        )
    command = applied_plan["commands"]["rebuild"]
    return {
        "verified": True,
        "strategy": "PROJECT_PROPERTY_SHEET",
        "configuration": applied_plan["configuration"],
        "platform": applied_plan["platform"],
        "command": {
            "executable": command["executable"],
            "arguments": command["arguments"],
            "workingDirectory": command["workingDirectory"],
        },
        "transientGlobalPropertiesAbsent": [
            "ForceImportBeforeCppTargets",
            "CoverWrapperDirectory",
        ],
        "wrapperVerification": wrapper_verifications,
        "artifactVerification": artifact_result,
        "outputPathComparison": comparison,
        "buildInterval": {
            "startedAt": command_results[0]["startedAt"],
            "finishedAt": command_results[-1]["finishedAt"],
        },
    }
