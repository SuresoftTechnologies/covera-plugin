"""End-to-end C# Toolbox coverage workflow."""

from __future__ import annotations

from dataclasses import dataclass

import re
import time
from pathlib import Path
from typing import Any, Callable

import project_index

from cover_build.pipelines.visual_studio import tools as visual_studio
from cover_build.core import state_store
from cover_build.core.job_evidence import with_output_tracking
from cover_build.core.errors import WorkflowWaiting
from cover_build.planning import build_plan
from cover_build.pipelines.managed_common import (
    _absolute_entry,
    _command_spec,
    _executable,
    _managed_plan,
    _provided_plan,
    _resolve_artifacts,
    _run,
    _select,
)
from cover_build.services import agent_open_api as agent
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors
from . import project_scope

from . import toolbox as toolbox_support


def _selected_csharp_projects(entry: Path, project_root: Path) -> list[Path]:
    if entry.suffix.casefold() == ".csproj":
        return [entry]
    projects: list[Path] = []
    expression = re.compile(r'["\']([^"\']+\.csproj)["\']', re.IGNORECASE)
    try:
        lines = entry.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:
        raise errors.ExecutorError("CSHARP_SOLUTION_READ_FAILED", str(exc), [str(entry)]) from exc
    for line in lines:
        match = expression.search(line)
        if not match:
            continue
        candidate = pathutil.canonical(entry.parent / match.group(1), False)
        if candidate.is_file() and pathutil.path_within(candidate, project_root):
            if not any(pathutil.same_path(candidate, current) for current in projects):
                projects.append(candidate)
    if not projects:
        raise errors.ExecutorError(
            "CSHARP_PROJECT_SCOPE_NOT_RESOLVED",
            "The selected solution did not resolve any in-scope C# project",
            [str(entry)],
        )
    return projects


def _requires_project_assets(project: Path) -> bool:
    text = project.read_text(encoding="utf-8-sig", errors="replace")
    return bool(re.search(r"<Project\b[^>]*\bSdk\s*=|<(?:TargetFramework|TargetFrameworks|PackageReference)\b", text, re.IGNORECASE))


def _restore_asset_evidence(projects: list[Path], scope=None) -> dict[str, Any]:
    if scope is not None:
        unresolved = [f"{item['project']} ({item.get('targetFramework') or 'unspecified framework'})"
                      for item in scope if item['requiresAssets'] and not item['assetsFile']]
        if unresolved:
            raise errors.ExecutorError('CSHARP_PROJECT_SCOPE_NOT_RESOLVED', 'ProjectAssetsFile could not be evaluated', unresolved)
        required = list(dict.fromkeys(Path(item['assetsFile']) for item in scope if item['requiresAssets']))
    else:
        required = [project.parent / "obj" / "project.assets.json" for project in projects if _requires_project_assets(project)]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise errors.ExecutorError(
            "CSHARP_RESTORE_ASSETS_MISSING",
            "C# restore completed but required project.assets.json files are missing",
            [str(path) for path in missing],
        )
    return {
        "requiredProjectAssets": [toolbox_support._file_snapshot(path) for path in required],
        "legacyProjects": [str(project) for project in projects if not _requires_project_assets(project)],
    }


def _csharp_restore_spec(
    driver: str,
    executable: Path,
    entry: Path,
    configuration: str | None,
    build_specification: dict[str, Any],
) -> dict[str, Any]:
    """Use the selected managed build tool to restore before the diagnostic build."""
    if driver == "CSHARP_DOTNET":
        arguments = ["restore", str(entry), "--verbosity", "minimal"]
    elif driver == "CSHARP_MSBUILD":
        arguments = [str(entry), "/t:Restore"]
        if configuration:
            arguments.append(f"/p:Configuration={configuration}")
        arguments.append("/v:minimal")
    else:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"Unsupported C# restore driver: {driver}")
    restore = dict(build_specification)
    restore["executable"] = str(executable)
    restore["arguments"] = arguments
    restore["coverToolEnvironment"] = False
    return restore


def _csharp_build_tool(system: str, choices: dict[str, Any]) -> Path:
    """Resolve one C# build tool without changing the selected build system."""
    selected = choices.get("managedBuildTool")
    legacy_msbuild = choices.get("msbuildPath") if system == "MSBUILD" else None
    if selected is not None and legacy_msbuild is not None and not pathutil.same_path(selected, legacy_msbuild):
        raise errors.ExecutorError(
            "MANAGED_BUILD_TOOL_SELECTION_CONFLICT",
            "managedBuildTool and msbuildPath select different C# MSBuild executables",
            [str(selected), str(legacy_msbuild)],
        )
    if system == "DOTNET":
        return _executable("dotnet", selected)
    # ``msbuildPath`` is emitted by the shared Visual Studio selector. Accept
    # it as a resume-compatible alias, then keep the resolved path in the C#
    # original-build plan so the next resume has one authoritative tool.
    try:
        return visual_studio.find_tools(selected or legacy_msbuild)[0]
    except WorkflowWaiting as exc:
        if exc.code != "VISUAL_STUDIO_INSTALLATION_SELECTION_REQUIRED":
            raise
        raise WorkflowWaiting(exc.code, "Select the Visual Studio installation normally used for this solution.",
                              exc.choices, decision_owner="USER", purpose="BUILD_ENVIRONMENT",
                              evidence=exc.evidence) from exc


def _log_indicates_nuget_config_access_denied(log: Path) -> bool:
    try:
        text = processes.read_output_tail(log)
    except OSError:
        return False
    normalized = text.casefold()
    return "nuget.config" in normalized and any(
        marker in normalized
        for marker in ("access is denied", "access to the path", "permission denied", "unauthorizedaccessexception", "액세스가 거부")
    )


def _log_indicates_transient_output_lock(log: Path) -> bool:
    try:
        text = processes.read_output_tail(log)
    except OSError:
        return False
    normalized = text.casefold()
    return "cs2012" in normalized and any(
        marker in normalized
        for marker in ("used by another process", "process cannot access the file", "다른 프로세스", "사용 중")
    )


def _run_csharp_command(
    specification: dict[str, Any],
    log: Path,
    failure_code: str,
    *,
    retry_transient_output_lock: bool,
) -> dict[str, Any]:
    """Run one C# command without silently changing job, driver, or outputs."""
    try:
        return _run(specification, log, failure_code)
    except errors.ExecutorError as first_error:
        if _log_indicates_nuget_config_access_denied(log):
            raise WorkflowWaiting(
                "CSHARP_HOST_ACCESS_REQUIRED",
                "The selected C# restore cannot read the current user's NuGet.Config. Grant host access, then resume this same job.",
                {"resumeAfterHostAccessApproval": True},
                decision_owner="USER",
                purpose="MUTATION_PERMISSION",
                evidence=[str(log)],
            ) from first_error
        if not retry_transient_output_lock or not _log_indicates_transient_output_lock(log):
            raise
        retry_log = log.with_name(f"{log.stem}-retry-1{log.suffix}")
        # A known CS2012 sharing violation is external to the project. Retry
        # once, in the same working/output directory, without cleaning or
        # deleting the locked file.
        time.sleep(2)
        try:
            result = _run(specification, retry_log, failure_code)
        except errors.ExecutorError as retry_error:
            raise errors.ExecutorError(
                failure_code,
                "C# build still failed after one transient output-lock retry",
                [str(log), str(retry_log)],
            ) from retry_error
        result["retry"] = {
            "reason": "CS2012_TRANSIENT_OUTPUT_LOCK",
            "attemptCount": 2,
            "firstAttemptLog": str(log),
        }
        return result


def _plan_with_restore(
    job: dict[str, Any], plan: dict[str, Any], restore_specification: dict[str, Any]
) -> dict[str, Any]:
    if "restore" in plan["commands"]:
        return plan
    raw = {key: value for key, value in plan.items() if key != "integrity"}
    raw["commands"] = {**raw["commands"], "restore": restore_specification}
    return build_plan.validate_original_build_plan(job, raw, str(raw["driver"]))


def _csharp_compiler_observation(log: Path, choices: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    exe_expression = re.compile(
        r'(?:["\'])([^"\']*csc\.exe)(?:["\'])|((?:[A-Za-z]:\\|/)[^\s]*csc\.exe)',
        re.IGNORECASE,
    )
    dll_expression = re.compile(r'((?:[A-Za-z]:\\|/)[^\s"\']*[/\\]csc\.dll)', re.IGNORECASE)
    server_expression = re.compile(r'(?:VBCSCompiler|compiler server|UseSharedCompilation)', re.IGNORECASE)
    executables: list[Path] = []
    csc_dlls: list[Path] = []
    server_observed = False
    for line in processes.iter_output_lines(log):
        server_observed = server_observed or bool(server_expression.search(line))
        for match in exe_expression.finditer(line):
            raw = match.group(1) or match.group(2)
            if raw and Path(raw).is_file():
                value = pathutil.canonical(raw)
                if not any(pathutil.same_path(value, current) for current in executables):
                    executables.append(value)
        for raw in dll_expression.findall(line):
            if Path(raw).is_file():
                value = pathutil.canonical(raw)
                if not any(pathutil.same_path(value, current) for current in csc_dlls):
                    csc_dlls.append(value)
    supplied = choices.get("cscPath")
    if isinstance(supplied, str) and supplied:
        selected = pathutil.canonical(supplied)
        if selected.name.casefold() != "csc.exe":
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "cscPath must identify csc.exe")
    elif len(executables) == 1:
        selected = executables[0]
    else:
        raise WorkflowWaiting(
            "CSHARP_COMPILER_SELECTION_REQUIRED",
            "The original diagnostic evidence did not resolve one Toolbox-compatible csc.exe",
            {"cscPath": [str(value) for value in executables]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[str(log), *[str(value) for value in csc_dlls]],
        )
    if executables:
        mode = "PROCESS_EXE"
    elif csc_dlls:
        mode = "DOTNET_CSC_DLL"
    elif server_observed:
        mode = "COMPILER_SERVER"
    else:
        mode = "IN_PROCESS"
    return selected, {
        "kind": "CSHARP",
        "compilerMode": mode,
        "selectedCompiler": str(selected),
        "observedExecutables": [str(value) for value in executables],
        "observedCompilerAssemblies": [str(value) for value in csc_dlls],
        "compilerServerObserved": server_observed,
        "evidence": [str(log)],
    }

def _csharp_instrumentation_proof(log: Path, projects: list[Path], csc: Path) -> dict[str, Any]:
    hooker_observed = processes.output_contains(log, r"QualityScroll\.Cover\.CscHooker(?:\.exe)?", re.IGNORECASE)
    wrapper_observed = processes.output_contains(log, r"\.cs_wrapper[/\\][^\s]+\.cmd", re.IGNORECASE)
    project_results: list[dict[str, Any]] = []
    log_text = log.read_text(encoding="utf-8", errors="replace").casefold()
    for project in projects:
        text = project.read_text(encoding="utf-8-sig", errors="replace")
        wrapper_directory = project.parent / ".cs_wrapper"
        commands = sorted(wrapper_directory.glob("*.cmd")) if wrapper_directory.is_dir() else []
        hooker_in_command = False
        wrapper_invoked = False
        for command in commands:
            command_text = command.read_text(encoding="utf-8-sig", errors="replace")
            hooker_in_command = hooker_in_command or bool(
                re.search(r"QualityScroll\.Cover\.CscHooker(?:\.exe)?", command_text, re.IGNORECASE)
            )
            wrapper_invoked = wrapper_invoked or str(command).casefold() in log_text
        # MakeCmdFile creates a wrapper that invokes CscHooker. The original
        # compiler path is correctly stored in ProjectExtensions/Cover/
        # OrgCscPath, then re-used by CscHooker; it is not expected in .cmd.
        csc_in_project = str(csc).casefold() in text.casefold()
        configured = (
            ".cs_wrapper" in text
            and "CscToolPath" in text
            and "CscToolExe" in text
            and "OrgCscPath" in text
            and bool(commands)
            and hooker_in_command
            and csc_in_project
            and wrapper_invoked
        )
        project_results.append({
            "project": str(project),
            "configured": configured,
            "wrapperDirectory": str(wrapper_directory),
            "wrapperCommands": [str(value) for value in commands],
            "wrapperInvocationObserved": wrapper_invoked,
            "cscHookerReferenced": hooker_in_command,
            "originalCompilerConfigured": csc_in_project,
        })
    failed = [item["project"] for item in project_results if not item["configured"]]
    if failed or not hooker_observed or not wrapper_observed:
        raise errors.ExecutorError(
            "CSHARP_INSTRUMENTATION_NOT_PROVED",
            "The coverage build did not prove Toolbox/CscHooker processing for every selected C# project",
            [str(log), *failed],
        )
    return {
        "kind": "CSHARP_TOOLBOX",
        "hookerInvocationObserved": hooker_observed,
        "wrapperInvocationObserved": wrapper_observed,
        "projects": project_results,
        "allSelectedProjectsConfigured": True,
        "evidence": [str(log)],
    }

@dataclass(frozen=True, kw_only=True)
class CsharpOriginalPlan:
    """선택한 C# 구성과 restore·원본 빌드 계획."""
    configuration: Any
    driver: Any
    entry: Any
    frameworks: Any
    plan: Any
    restore_specification: Any
    spec: Any


def _prepare_original_plan(
    *, choices, evidence, job
) -> CsharpOriginalPlan:
    data = evidence["csharp"]
    provided = _provided_plan(job, choices, {"CSHARP_MSBUILD", "CSHARP_DOTNET"})
    if provided is not None:
        plan = provided
        driver = str(plan["driver"])
        system = "DOTNET" if driver == "CSHARP_DOTNET" else "MSBUILD"
        entry = Path(plan["adapterEntry"])
        if entry.suffix.lower() in {".sln", ".slnx"}:
            if system != "MSBUILD":
                raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "C# solutions require Visual Studio MSBuild.exe")
            tool = _csharp_build_tool(system, {**choices, "managedBuildTool": plan["buildTool"]})
            if any(not pathutil.same_path(cmd["executable"], tool)
                   for phase, cmd in plan["commands"].items() if phase in {"build", "restore"}):
                raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "Solution restore and build must use the selected MSBuild.exe")
        configuration = plan.get("configuration")
        frameworks = list(plan.get("targetFrameworks") or [])
        executable = Path(plan["buildTool"])
        spec = dict(plan["commands"]["build"])
    else:
        entries = list(data.get("solutions") or data.get("projectFiles") or [])
        entry = _absolute_entry(Path(job["projectRoot"]), _select("managedBuildEntry", entries, choices))
        systems = list(data.get("confirmedBuildSystems") or data.get("buildSystemCandidates") or [])
        if entry.suffix.lower() in {".sln", ".slnx"}:
            systems = ["MSBUILD"]
        system = _select("managedBuildSystem", systems, choices)
        command_configs = sorted({str(item["configuration"]) for item in data.get("commandEvidence") or [] if item.get("configuration")})
        configuration = choices.get("managedConfiguration") or (command_configs[0] if len(command_configs) == 1 else None)
        if not isinstance(configuration, str) or not configuration:
            raise WorkflowWaiting(
                "CSHARP_CONFIGURATION_REQUIRED",
                "The C# build configuration is not evidenced",
                {"managedConfiguration": command_configs},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
            )
        frameworks = choices.get("targetFrameworks") or data.get("selectedTargetFrameworks") or []
        if not isinstance(frameworks, list) or not all(isinstance(value, str) for value in frameworks):
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", "targetFrameworks must be a string array")
        if system == "DOTNET":
            executable = _csharp_build_tool(system, choices)
            arguments = ["build", str(entry), "--configuration", configuration, "--no-restore", "--no-incremental", "--verbosity", "diagnostic"]
            if len(frameworks) == 1:
                arguments.extend(["--framework", frameworks[0]])
            driver = "CSHARP_DOTNET"
        else:
            executable = _csharp_build_tool(system, choices)
            arguments = [str(entry), "/t:Rebuild", f"/p:Configuration={configuration}", "/v:diag"]
            if len(frameworks) == 1:
                arguments.append(f"/p:TargetFramework={frameworks[0]}")
            driver = "CSHARP_MSBUILD"
        spec = _command_spec(executable, arguments, entry.parent)
        restore_specification = _csharp_restore_spec(driver, executable, entry, configuration, spec)
        plan = _managed_plan(
            job, driver, executable, entry, spec, configuration, [], frameworks, "CSHARP_TOOLBOX",
            restore_specification=restore_specification,
        )
    restore_specification = dict(plan["commands"].get("restore") or _csharp_restore_spec(
        driver, executable, entry, configuration, spec
    ))
    plan = _plan_with_restore(job, plan, restore_specification)
    plan = with_output_tracking(plan, choices)
    return CsharpOriginalPlan(
        configuration=configuration,
        driver=driver,
        entry=entry,
        frameworks=frameworks,
        plan=plan,
        restore_specification=restore_specification,
        spec=spec,
    )


@dataclass(frozen=True, kw_only=True)
class CsharpToolboxEnabled:
    """Toolbox 설정과 래퍼 형식 확인을 마친 프로젝트 목록."""
    changed_projects: Any


def _enable_coverage_toolbox(
    *, before_enable, entry, job, project_paths, record, stages, toolbox
) -> CsharpToolboxEnabled:
    enable = toolbox_support._run_toolbox_command(
        toolbox_support._toolbox_command_spec(toolbox, ["enable", str(entry)], entry.parent, coverage=True),
        Path(job["directories"]["logs"]) / "csharp-toolbox-enable.log",
        "CSHARP_TOOLBOX_ENABLE_FAILED",
    )
    after_enable = toolbox_support._toolbox_state(project_paths)
    changed_projects = after_enable["configuredProjects"]
    missing_projects = [
        item["project"]["path"]
        for item in after_enable["projects"]
        if not item["configured"]
    ]
    if missing_projects:
        raise errors.ExecutorError(
            "CSHARP_TOOLBOX_ENABLE_INCOMPLETE",
            "Toolbox exited without configuring every selected C# project",
            [str(enable["log"]), *missing_projects],
        )
    bom_wrappers = toolbox_support._utf8_bom_wrappers(after_enable)
    if bom_wrappers:
        raise errors.ExecutorError(
            "CSHARP_TOOLBOX_WRAPPER_BOM",
            "C# Toolbox generated UTF-8-BOM batch wrappers even in a hidden native Windows console; deploy a product-side MakeCmdFile encoding fix before retrying",
            [str(enable["log"]), *bom_wrappers],
        )
    record(
        job,
        stages,
        10,
        "csharp-toolbox-enable",
        envelopes.envelope(
            "ENABLING_CSHARP_TOOLBOX",
            "SUCCEEDED",
            {
                "results": [enable],
                "changedProjects": changed_projects,
                "before": before_enable,
                "after": after_enable,
            },
        ),
    )
    before_path = Path(job["directories"]["temporary"]) / "csharp-artifacts-before.json"
    after_path = Path(job["directories"]["temporary"]) / "csharp-artifacts-after.json"
    return CsharpToolboxEnabled(
        changed_projects=changed_projects,
    )


def run_csharp(
    job: dict[str, Any], evidence: dict[str, Any], choices: dict[str, Any], stages: list[dict[str, Any]],
    args: Any, record: Callable[..., dict[str, Any]], ensure_authentication: Callable[[], None],
    finalize_survey: Callable[..., dict[str, Any]],
    write_original_evidence: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    prepared = _prepare_original_plan(
        choices=choices,
        evidence=evidence,
        job=job,
    )
    configuration = prepared.configuration
    driver = prepared.driver
    entry = prepared.entry
    frameworks = prepared.frameworks
    plan = prepared.plan
    restore_specification = prepared.restore_specification
    spec = prepared.spec
    record(job, stages, 4, "resolve-original-build-command", envelopes.envelope("RESOLVING_ORIGINAL_BUILD_COMMAND", "SUCCEEDED", {"plan": plan}))
    finalize_survey(job, stages, plan)
    if not args.execute:
        raise WorkflowWaiting("ORIGINAL_BUILD_EXECUTION_REQUIRED", "The original C# build must run", {"execute": True}, decision_owner="USER", purpose="MUTATION_PERMISSION")
    project_paths = _selected_csharp_projects(entry, Path(job["projectRoot"]))
    toolbox = toolbox_support._toolbox_executable()
    original_toolbox_state = toolbox_support._disable_toolbox_if_configured(
        toolbox,
        entry,
        project_paths,
        Path(job["directories"]["logs"]) / "csharp-toolbox-disable-before-original.log",
    )
    record(
        job,
        stages,
        4,
        "csharp-toolbox-disable-before-original",
        envelopes.envelope(
            "DISABLING_CSHARP_TOOLBOX_FOR_ORIGINAL_BUILD",
            "SUCCEEDED",
            original_toolbox_state,
        ),
    )
    scope = project_scope.resolve(entry, project_paths, configuration, spec, driver, Path(job['directories']['logs']))
    build_projects = list(dict.fromkeys(Path(item['project']) for item in scope))
    record(job, stages, 4, 'resolve-csharp-project-scope', envelopes.envelope(
        'RESOLVING_CSHARP_PROJECT_SCOPE', 'SUCCEEDED', {'projects': scope}))
    restore = state_store.run_checkpointed(
        job, stages, record, 5, "execute-csharp-restore",
        {"plan": plan, "toolboxState": original_toolbox_state["after"], "projectScope": [
            {key: value for key, value in item.items() if key != 'evidence'} for item in scope]},
        lambda: envelopes.envelope(
            "EXECUTING_CSHARP_RESTORE", "SUCCEEDED",
            {
                "driver": driver,
                "command": _run_csharp_command(
                    restore_specification,
                    Path(job["directories"]["logs"]) / "original-csharp-restore.log",
                    "CSHARP_RESTORE_FAILED",
                    retry_transient_output_lock=False,
                ),
                "assets": _restore_asset_evidence(build_projects, scope),
            },
        ),
    )
    _restore_asset_evidence(build_projects, scope)
    original = state_store.run_checkpointed(
        job, stages, record, 6, "execute-original-build",
        {"plan": plan, "projectIndexFingerprint": project_index.input_fingerprint(fileio.load_json(Path(job["directories"]["temporary"]) / "project-index.json"))},
        lambda: envelopes.envelope(
            "EXECUTING_ORIGINAL_BUILD", "SUCCEEDED",
            {
                "driver": driver,
                "originalBuildPlan": plan,
                "command": _run_csharp_command(
                    spec,
                    Path(job["directories"]["logs"]) / "original-csharp.log",
                    "ORIGINAL_BUILD_FAILED",
                    retry_transient_output_lock=True,
                ),
            },
        ),
    )
    command = original["data"]["command"]
    original["data"]["restore"] = restore["data"]
    csc, compiler_observation = _csharp_compiler_observation(Path(command["log"]), choices)
    artifacts = _resolve_artifacts(job, choices, command, {".dll", ".exe"})
    original["data"]["toolchain"] = {"compiler": str(csc)}
    original["data"]["artifacts"] = [{"path": str(path)} for path in artifacts]
    write_original_evidence(
        job,
        stages,
        envelopes.envelope(
            "CAPTURING_ORIGINAL_BUILD_EVIDENCE",
            "SUCCEEDED",
            {
                "driver": driver,
                "originalBuildPlan": plan,
                "commands": {"restore": restore["data"]["command"], "build": command},
                "toolchain": {
                    "family": "CSHARP",
                    "tools": {"compiler": str(csc)},
                    "evidence": {"compiler": [command["log"]]},
                    "observation": compiler_observation,
                },
                "artifacts": [{"path": str(path)} for path in artifacts],
            },
        ),
    )

    ensure_authentication()
    agent.ensure_wrapper_mappings(
        job, stages, record, args.allow_agent_start, capability="CSHARP",
        cover_user_name=choices.get("coverUserName"),
    )
    config_state = toolbox_support._prepare_csharp_toolbox_config(job, csc)
    record(
        job,
        stages,
        10,
        "csharp-toolbox-config-apply",
        envelopes.envelope("CONFIGURING_CSHARP_TOOLBOX", "SUCCEEDED", config_state),
    )
    before_enable = toolbox_support._toolbox_state(project_paths)
    primary_error: BaseException | None = None
    try:
        enabled = _enable_coverage_toolbox(
            before_enable=before_enable,
            entry=entry,
            job=job,
            project_paths=project_paths,
            record=record,
            stages=stages,
            toolbox=toolbox,
        )
        changed_projects = enabled.changed_projects
        coverage_spec = dict(spec)
        coverage_spec["coverToolEnvironment"] = True
        coverage_spec["instrumentationLanguage"] = "CSHARP"
        coverage = _run_csharp_command(
            coverage_spec,
            Path(job["directories"]["logs"]) / "coverage-csharp.log",
            "COVERAGE_BUILD_FAILED",
            retry_transient_output_lock=True,
        )
        coverage_artifacts = artifacts
        build_result = {
            "results": [coverage], "artifacts": [str(path) for path in coverage_artifacts],
            "successCriterion": "BUILD_EXIT_CODE",
            "buildInterval": {"startedAt": coverage["startedAt"], "finishedAt": coverage["finishedAt"]},
        }
        record(job, stages, 13, "csharp-coverage-build", envelopes.envelope("EXECUTING_CSHARP_COVERAGE_BUILD", "SUCCEEDED", build_result))
        return {
            **build_result,
            "buildSucceeded": True,
            "_manualCoverage": {
                "buildSystem": "CSHARP",
                "commands": [restore_specification, coverage_spec],
                "assets": [],
                "artifacts": [str(path) for path in coverage_artifacts],
                "mode": "COVER_TOOLBOX_ENABLED",
                "details": {
                    "entry": str(entry),
                    "configuration": configuration,
                    "targetFrameworks": frameworks,
                    "configuredProjects": changed_projects,
                    "disableCommand": [str(toolbox), "disable", str(entry)],
                },
            },
        }
    except BaseException as error:
        primary_error = error
        cleanup = toolbox_support._disable_toolbox_if_configured(
            toolbox,
            entry,
            project_paths,
            Path(job["directories"]["logs"]) / "csharp-toolbox-disable-after-failure.log",
        )
        record(
            job,
            stages,
            28,
            "csharp-toolbox-disable-after-failure",
            envelopes.envelope(
                "RESTORING_CSHARP_PROJECTS_WITH_TOOLBOX",
                "SUCCEEDED",
                cleanup,
            ),
        )
        raise
    finally:
        config_restore = toolbox_support._restore_csharp_toolbox_config(config_state)
        restore_status = "SUCCEEDED" if config_restore["restored"] else "FAILED"
        record(
            job,
            stages,
            28,
            "csharp-toolbox-config-restore",
            envelopes.envelope("RESTORING_CSHARP_TOOLBOX_CONFIG", restore_status, config_restore),
        )
        if not config_restore["restored"] and primary_error is None:
            raise errors.ExecutorError(
                "CSHARP_TOOLBOX_CONFIG_RESTORE_FAILED",
                "Toolbox C# configuration could not be restored without overwriting a concurrent change",
                [str(config_state["path"])],
            )


run = run_csharp
