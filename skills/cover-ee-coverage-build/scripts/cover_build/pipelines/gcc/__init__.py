"""End-to-end coverage workflow for this build environment."""

from __future__ import annotations

from dataclasses import dataclass

import argparse
import copy
import os
from pathlib import Path
from typing import Any


from cover_build.pipelines.gcc import selection as gcc
from cover_build.core import state_store
from cover_build.core.job_evidence import with_output_tracking
from cover_build.core.errors import WorkflowWaiting
from cover_build.planning import build_plan
from cover_build.common.jobs import argument
from cover_build.planning.resolution import fallback_source, resolve_fallback_artifact
from cover_build.core.job_evidence import finalize_environment_survey_plan, job_project_input_fingerprint, job_work_path, record, record_coverage_injection_evidence, write_original_build_evidence
from cover_build.services.gcc_coverage import probe_windows_gcc_ar_wrapper, selected_cover_config
from cover_build.common.fileio import write_json
from cover_build.services import agent_open_api as agent
from cover_build.services import gcc_coverage
from cover_build.pipelines.gcc import original as gcc_original
from cover_build.planning import toolchain_review
from cover_build.services import glue_compiler
from cover_build.common import processes as native_common
from cover_build.services import project_backup
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.planning import gcc_tools
from cover_build.planning import cmake_msbuild
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.services import toolchains
from cover_build.core import errors
from cover_build.services import build_execution



def preserve_original_environment(plan: dict[str, Any], original_plan: dict[str, Any]) -> dict[str, Any]:
    """Keep the validated build environment; coverage-specific variables take precedence."""
    result = copy.deepcopy(plan)
    originals = original_plan["commands"]
    build = originals.get("rebuild", originals.get("build", {}))
    for phase, command in result["commands"].items():
        source = originals.get(phase, build)
        if source.get("environmentMode") == "replace":
            command["environmentMode"] = "replace"
            command["environment"] = dict(source.get("environment", {}))
        merged = dict(source.get("environmentDelta", {}))
        for key, value in command.get("environmentDelta", {}).items():
            if os.name == "nt":
                for previous in list(merged):
                    if previous.casefold() == key.casefold():
                        del merged[previous]
            merged[key] = value
        command["environmentDelta"] = merged
    return fileio.add_integrity(result)



def _prepare_original_plan(job, discovery, build_system, choices, stages, args, build_evidence):
    build_evidence = build_evidence or build_plan.collect_build_evidence(job, discovery)
    raw_original_plan = choices.get("originalBuildPlan")
    selected_references_path = job_work_path(job, "temporary", "selected-build-references.json")
    likely_build_scripts = [
        script["path"]
        for script in build_evidence.get("buildScripts", [])
        if script.get("likelyBuildEntry") and isinstance(script.get("path"), str)
    ]
    # Resolve an available native entry before asking the model for its plan.
    # Script-only projects may generate that entry, so retain their early handoff.
    native_entries = discovery["data"].get("gccBuildDrivers", {}).get(build_system, [])
    if (raw_original_plan is None and likely_build_scripts
            and not choices.get("gccBuildEntry") and not native_entries):
        selected_references = build_plan.collect_selected_references(job, likely_build_scripts)
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
        evidence_path = job_work_path(job, "temporary", "build-evidence.json")
        raise WorkflowWaiting(
            "ORIGINAL_BUILD_PLAN_REQUIRED",
            "A customer build script must be interpreted before selecting and executing its native build entry",
            {"originalBuildPlan": [], "originalBuildCommandSource": ["MODEL_INFERRED"]},
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
            evidence=[str(evidence_path), str(selected_references_path)],
        )
    model_plan = (
        build_plan.validate_original_build_plan(job, raw_original_plan, build_system)
        if raw_original_plan is not None
        else None
    )
    planning_choices = dict(choices)
    if model_plan is not None:
        declared_source = fallback_source(choices)
        if declared_source != model_plan["origin"]:
            raise errors.ExecutorError(
                "INVALID_MODEL_BUILD_COMMAND",
                "originalBuildCommandSource must match originalBuildPlan.origin",
                [declared_source, model_plan["origin"]],
            )
        planning_choices["gccBuildEntry"] = model_plan["adapterEntry"]
    selection = gcc.select_gcc_build(
        job,
        discovery,
        build_system,
        planning_choices,
        explicit_build_directory=getattr(args, "gcc_build_directory", None),
        explicit_generator=getattr(args, "gcc_generator", None) or (model_plan or {}).get("generator"),
        original_plan=model_plan,
        explicit_configuration=getattr(args, "gcc_configuration", None),
    )
    selected_references = build_plan.collect_selected_references(
        job, list(dict.fromkeys([selection["entry"], *likely_build_scripts]))
    )
    # Carry the resolved route into the model handoff; scripts are evidence,
    # not permission to replace the selected toolchain or build entry.
    selected_references["selectedBuild"] = selection
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
    if raw_original_plan is None and build_plan.requires_model_original_plan(
        build_evidence, build_system, selection["entry"]
    ):
        evidence_path = Path(job["directories"]["temporary"]) / "build-evidence.json"
        raise WorkflowWaiting(
            "ORIGINAL_BUILD_PLAN_REQUIRED",
            "Interpret scripts, presets, and prerequisites for selectedBuild in the references evidence; preserve the selected entry and toolchain unless conflicting evidence requires an explicit revised decision",
            {
                "originalBuildPlan": [],
                "originalBuildCommandSource": ["MODEL_INFERRED"],
            },
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
            evidence=[str(evidence_path), str(selected_references_path)],
        )
    if model_plan is not None:
        if not pathutil.same_path(model_plan["adapterEntry"], selection["entry"]):
            raise errors.ExecutorError(
                "INVALID_ORIGINAL_BUILD_PLAN",
                "The original build plan adapterEntry conflicts with project selection",
                [model_plan["adapterEntry"], selection["entry"]],
            )
        selection["requiredRoles"] = list(model_plan["requiredRoles"])
        if model_plan.get("configuration"):
            selection["configuration"] = model_plan["configuration"]
    record(job, stages, 3, "select-gcc-driver", envelopes.envelope("SELECTING_GCC_BUILD_DRIVER", "SUCCEEDED", selection))
    build_tool = model_plan["buildTool"] if model_plan is not None else gcc.select_build_tool(selection["driver"], selection, choices, args.gcc_build_tool)
    configure_arguments = [
        *((model_plan or {}).get("coverageInjection", {}).get("configureArguments", [])),
        *args.gcc_configure_arg,
    ]
    if selection["driver"] == "CMAKE":
        gcc_tools.validate_cmake_configure_args(configure_arguments)
    original_plan = model_plan or gcc_original.create_gcc_original_plan(
        job,
        selection,
        build_tool,
        selection["preservedConfigureArgs"],
        list(args.target),
        args.parallel,
        args.gcc_clean_target,
        [str(Path(job["directories"]["temporary"]) / "build-evidence.json")],
        evidence_source="CACHE" if selection.get("cache") else "SCRIPT",
    )
    target_tool_paths = choices.get("targetToolPaths")
    if model_plan is None and target_tool_paths is not None:
        declared_source = fallback_source(choices)
        if declared_source not in {"MODEL_INFERRED", "USER_CONFIRMED_FALLBACK"} or not isinstance(target_tool_paths, dict):
            raise errors.ExecutorError(
                "INVALID_MODEL_BUILD_COMMAND",
                "targetToolPaths requires model/user fallback provenance and an object value",
            )
        revised_plan = {key: value for key, value in original_plan.items() if key != "integrity"}
        revised_plan["origin"] = declared_source
        revised_plan["targetTools"] = target_tool_paths
        revised_plan["evidence"] = [*original_plan["evidence"], "MODEL_SELECTED_TARGET_TOOLS"]
        original_plan = build_plan.validate_original_build_plan(job, revised_plan, selection["driver"])
    original_plan = with_output_tracking(original_plan, choices)
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
                "environmentInitialization": original_plan["environmentInitialization"],
                "driver": original_plan["driver"],
                "plan": str(original_plan_path),
                "commands": original_plan["commands"],
                "coverageInjection": original_plan["coverageInjection"],
                "evidence": original_plan["evidence"],
            },
        ),
    )
    finalize_environment_survey_plan(job, stages, original_plan)
    return build_evidence, selection, selected_references, build_tool, configure_arguments, original_plan



def _legacy_original_checkpoint_inputs(job, inputs, original_plan):
    candidates = [{**inputs, "plan": original_plan}]
    if not job.get("jobDirectory"):
        return tuple(candidates)
    # Old automatic plans duplicated original configure options in the
    # coverage-only field. Recover that old digest without changing commands.
    for path in (Path(job["jobDirectory"]) / "events").glob("*-*-execute-original-build.json"):
        try:
            event = fileio.load_json(path)
            if not isinstance(event, dict):
                continue
            result = event.get("result")
            if event.get("jobId") != job["jobId"] or not isinstance(result, dict) or result.get("status") != "SUCCEEDED":
                continue
            saved = result["data"]["originalBuildPlan"]
            if not isinstance(saved, dict):
                continue
            fileio.verify_integrity(saved)
            normalized = {key: value for key, value in saved.items() if key != "integrity"}
            normalized["coverageInjection"] = {**saved["coverageInjection"], "configureArguments": []}
            if normalized == inputs["plan"]:
                candidate = {**inputs, "plan": saved}
                if candidate not in candidates:
                    candidates.append(candidate)
        except (OSError, ValueError, KeyError, TypeError, errors.ExecutorError):
            continue
    return tuple(candidates)


def _build_and_review_original(job, choices, stages, args, selection, selected_references, build_tool, configure_arguments, original_plan):
    native_common.require_execution(args.execute)
    # Coverage-only recovery must not invalidate a successful original build.
    original_execution = {key: value for key, value in original_plan.items() if key != "integrity"}
    original_execution["coverageInjection"] = {**original_plan["coverageInjection"], "configureArguments": []}
    checkpoint_inputs = {
        "toolchainReviewVersion": 1,
        "plan": original_execution,
        "selection": selection,
        "selectedReferencesFingerprint": build_plan.selected_references_fingerprint(selected_references),
        "projectInputFingerprint": job_project_input_fingerprint(job),
    }
    native_build = state_store.run_checkpointed(
        job,
        stages,
        record,
        5,
        "execute-original-build",
        checkpoint_inputs,
        lambda: gcc_original.execute_gcc(
            job,
            selection,
            build_tool,
            configure_arguments,
            list(args.target),
            args.parallel,
            args.gcc_clean_target,
            original_plan=original_plan,
        ),
        # Match the old digest exactly; do not relax source, command, selection
        # or reference checks. Reuse is then recorded with the current digest.
        compatible_inputs=_legacy_original_checkpoint_inputs(job, checkpoint_inputs, original_plan),
    )
    write_original_build_evidence(job, stages, native_build)
    if selection["driver"] == "CMAKE":
        if not pathutil.same_path(selection["buildDirectory"], native_build["data"]["buildDirectory"]):
            raise errors.ExecutorError("COVERAGE_OUTPUT_PATH_CHANGED", "CMake selection differs from the observed original build directory")
        gcc.save_cmake_selection(job, selection)
        selection["generator"] = native_build["data"].get("generator") or selection.get("generator")
    toolchain = toolchain_review.resolve(job, native_build, choices.get("originalToolchainDecision"))
    native_build["data"]["toolchain"] = toolchain
    write_original_build_evidence(job, stages, native_build)
    record(
        job,
        stages,
        5,
        "extract-original-toolchain",
        envelopes.envelope("EXTRACTING_ORIGINAL_TOOLCHAIN", "SUCCEEDED", toolchain),
    )
    artifacts = list(getattr(args, "artifact", []))
    artifact_source = "EXPLICIT_ARGUMENT"
    if not artifacts:
        artifacts = gcc.infer_expected_artifacts(job, selection, native_build["data"])
        artifact_source = "ORIGINAL_BUILD_COMMAND_OUTPUT"
    record(
        job,
        stages,
        5,
        "infer-coverage-artifacts",
        envelopes.envelope(
            "INFERRING_COVERAGE_ARTIFACTS",
            "SUCCEEDED",
            {"source": artifact_source, "artifacts": artifacts},
        ),
    )
    wrapper_mapping, original_prefix = gcc_coverage.gcc_wrapper_mapping(toolchain)
    observed_tools = {role: Path(path) for role, path in toolchain["tools"].items()}
    mapping_provenance: dict[str, str] = {}
    for name, path in wrapper_mapping.items():
        observed_role = next(
            (role for role, observed in observed_tools.items() if pathutil.same_path(path, observed)),
            None,
        )
        mapping_provenance[name] = (
            str(toolchain.get("evidence", {}).get(observed_role, {}).get("provenance") or "ORIGINAL_BUILD_OBSERVED")
            if observed_role
            else "ORIGINAL_BUILD_OBSERVED_ALIAS"
        )
    record(
        job,
        stages,
        5,
        "resolve-coverage-toolchain",
        envelopes.envelope(
            "RESOLVING_COVERAGE_TOOLCHAIN",
            "SUCCEEDED",
            {
                "originalPrefix": original_prefix,
                "wrapperMappings": {name: str(path) for name, path in wrapper_mapping.items()},
                "provenance": mapping_provenance,
            },
        ),
    )
    return toolchain, artifacts, wrapper_mapping, original_prefix



def _prepare_agent_and_runtime(job, choices, stages, args, selection, original_plan, toolchain, wrapper_mapping, original_prefix, ensure_authentication):
    if ensure_authentication is not None:
        ensure_authentication()
    same_name_wrappers = selection["driver"] == "CMAKE" and cmake_msbuild.is_visual_studio(selection.get("generator"))
    if same_name_wrappers:
        wrapper_mapping = {name.lower(): path for name, path in wrapper_mapping.items()}
    descriptor = agent.ensure_wrapper_mappings(
        job,
        stages,
        record,
        args.allow_agent_start,
        required_wrappers=set(wrapper_mapping),
        real_tools_override=wrapper_mapping,
        allow_wrapper_aliases=not same_name_wrappers,
        mapping_change_approval=choices.get("wrapperMappingChangeApproval") if same_name_wrappers else None,
        archive_policy_approval=choices.get("archivePolicyApproval"),
        cover_user_name=choices.get("coverUserName"),
        toolchain_selection=choices.get("toolChainInfoSelection"),
        original_environment=gcc_original._command_environment(
            original_plan["commands"].get("rebuild", original_plan["commands"].get("build"))
        ),
    )
    if "resolvedWrapperMappings" in descriptor:
        wrapper_mapping = {name: Path(path) for name, path in descriptor["resolvedWrapperMappings"].items()}
        record(job, stages, 8, "resolve-existing-wrapper-mappings", envelopes.envelope(
            "RESOLVING_COVERAGE_TOOLCHAIN", "SUCCEEDED",
            {"wrapperMappings": descriptor["resolvedWrapperMappings"], "selection": "ORIGINAL_TOOL_PATH_AND_ROLE"},
        ))
    wrappers = gcc.select_wrapper_set(
        descriptor,
        choices,
        expected_prefix=original_prefix,
        toolchain=toolchain,
        wrapper_mapping=wrapper_mapping,
    )
    requested_cover_config = selected_cover_config(args, selection["driver"])
    observed_tool_directories = sorted(
        {
            str(Path(path).expanduser().resolve().parent)
            for path in (toolchain.get("invokedTools") or toolchain["tools"]).values()
            if Path(path).expanduser().is_absolute()
        }
    )
    glue_runtime = glue_compiler.prepare_runtime(
        job,
        toolchain,
        descriptor["agentRoot"],
        requested_cover_config,
        search_directories=observed_tool_directories,
        selected_compiler=choices.get("glueCodeCompiler"),
    )
    record(
        job,
        stages,
        9,
        "resolve-glue-code-compiler",
        envelopes.envelope("RESOLVING_GLUE_CODE_COMPILER", "SUCCEEDED", glue_runtime),
    )
    role_real_tools = dict(toolchain.get("invokedTools") or toolchain["tools"])
    coverage_required_roles = sorted(
        role for role in role_real_tools if role in {"c_compiler", "cxx_compiler", "ar", "linker"}
    )
    record(
        job,
        stages,
        9,
        "verify-toolchain",
        toolchains.verify_toolchain(
            argument(
                agent_root=descriptor["agentRoot"],
                tool=[f"{name}={path}" for name, path in wrapper_mapping.items()],
            )
        ),
    )
    return descriptor, wrappers, glue_runtime, role_real_tools, coverage_required_roles



@dataclass(frozen=True, kw_only=True)
class GccCoveragePlan:
    """명령행 주입 계획과 프로젝트 파일 대체 처리에 필요한 계획값."""
    ar_probe: Any
    common: Any
    coverage_clean_target: Any
    coverage_force_rebuild: Any
    coverage_targets: Any
    plan: Any
    plan_path: Any


def _prepare_coverage_plan(
    *, args, artifacts, build_tool, configure_arguments, coverage_required_roles, descriptor, glue_runtime, job, original_plan, role_real_tools, selection, stages, wrappers, option_recovery=None
) -> GccCoveragePlan:
    ar_probe = None
    plan_path = Path(job["directories"]["temporary"]) / "build-plan.json"
    coverage_targets = list(original_plan.get("targets") or [])
    coverage_force_rebuild = bool(original_plan["coverageInjection"].get("forceRebuild", False))
    coverage_clean_target = original_plan["coverageInjection"].get("cleanTarget")
    if not coverage_force_rebuild and not coverage_clean_target:
        coverage_clean_target = args.gcc_clean_target
    common = {
        "job_context": job,
        "option_recovery": option_recovery,
        "verified_coverage_options": getattr(args, "verified_coverage_options", None),
        "original_plan": original_plan,
        "project_root": job["projectRoot"],
        "c_compiler": wrappers.get("c_compiler"),
        "cxx_compiler": wrappers.get("cxx_compiler"),
        "ar": wrappers.get("ar"),
        "linker": wrappers.get("linker"),
        "cover_config": glue_runtime["coverConfig"],
        "glue_code_compiler": glue_runtime["path"],
        "glue_compiler_provenance": glue_runtime["provenance"],
        "artifact": artifacts,
        "parallel": args.parallel,
        "require_role": coverage_required_roles,
        "real_tool": [f"{role}={path}" for role, path in role_real_tools.items()],
        "timeout": 3600,
        "output": str(plan_path),
    }
    if selection["driver"] == "CMAKE":
        plan = gcc_tools.plan_cmake(
            argument(
                **common,
                cmake=build_tool,
                source=selection["sourceDirectory"],
                preserve_source_spelling=False,
                build=selection["buildDirectory"],
                generator=selection["generator"],
                configuration=args.gcc_configuration or selection["configuration"],
                configure_arg=configure_arguments,
                target=coverage_targets,
            )
        )
    elif selection["driver"] == "MAKE":
        ar_probe = (
            probe_windows_gcc_ar_wrapper(
                wrappers["ar"],
                descriptor["agentRoot"],
                glue_runtime["coverConfig"],
            )
            if "ar" in role_real_tools and "ar" in wrappers
            else {"applicable": False, "fallbackRequired": False, "reason": "ARCHIVER_NOT_USED_BY_ORIGINAL_BUILD"}
        )
        record(job, stages, 10, "probe-gcc-ar-wrapper", envelopes.envelope("PROBING_GCC_AR_WRAPPER", "SUCCEEDED", ar_probe))
        plan = gcc_tools.plan_make(
            argument(
                **common,
                make=build_tool,
                makefile=selection["entry"],
                working_directory=selection["buildDirectory"],
                clean_target=coverage_clean_target,
                force_rebuild=coverage_force_rebuild,
                tool_variable=[
                    f"{role}={name}"
                    for role, name in original_plan["coverageInjection"].get("toolVariables", {}).items()
                ],
                allow_native_ar_fallback=ar_probe["fallbackRequired"],
                target=coverage_targets,
            )
        )
    else:
        raise errors.ExecutorError(
            "UNSUPPORTED_BUILD_SYSTEM",
            f"Unsupported GCC build driver: {selection['driver']}",
        )
    plan["data"] = preserve_original_environment(plan["data"], original_plan)
    fileio.atomic_write_json(plan_path, plan["data"])
    record(job, stages, 11, f"plan-{selection['driver'].lower()}", plan)
    return GccCoveragePlan(
        ar_probe=ar_probe,
        common=common,
        coverage_clean_target=coverage_clean_target,
        coverage_force_rebuild=coverage_force_rebuild,
        coverage_targets=coverage_targets,
        plan=plan,
        plan_path=plan_path,
    )


def _build_coverage(job, stages, args, selection, original_plan, artifacts, descriptor, wrappers, glue_runtime, role_real_tools, coverage_required_roles, build_tool, configure_arguments, selected_references, build_evidence, *, option_recovery=None):
    prepared = _prepare_coverage_plan(
        args=args,
        artifacts=artifacts,
        build_tool=build_tool,
        configure_arguments=configure_arguments,
        coverage_required_roles=coverage_required_roles,
        descriptor=descriptor,
        glue_runtime=glue_runtime,
        job=job,
        original_plan=original_plan,
        role_real_tools=role_real_tools,
        selection=selection,
        stages=stages,
        wrappers=wrappers,
        option_recovery=option_recovery,
    )
    ar_probe = prepared.ar_probe
    common = prepared.common
    coverage_clean_target = prepared.coverage_clean_target
    coverage_force_rebuild = prepared.coverage_force_rebuild
    coverage_targets = prepared.coverage_targets
    plan = prepared.plan
    plan_path = prepared.plan_path
    injection_state: dict[str, Any] = {
        "sequence": ["COMMAND_LINE", "PROJECT_FILE_PATCH"],
        "commandLine": {"status": "RUNNING", "plan": str(plan_path)},
        "projectFilePatch": {"status": "NOT_USED"},
    }
    manual_plan = plan["data"]
    persistent_patch_records: list[dict[str, Any]] = []
    try:
        build_result = gcc_coverage.execute_gcc_plan(
            job,
            stages,
            record,
            plan_path,
            plan["data"]["adapter"],
            artifacts,
            args.execute,
        )
        injection_state["commandLine"]["status"] = "SUCCEEDED"
    except errors.ExecutorError as command_line_error:
        fallback_codes = {"CMAKE_WRAPPER_CACHE_MISMATCH", "GCC_WRAPPER_NOT_SELECTED", "GCC_REAL_TOOL_BYPASS", "GCC_NATIVE_TOOL_NOT_SELECTED"}
        diagnostic_result: dict[str, Any] | None = None
        if selection["driver"] == "MAKE" and command_line_error.code in fallback_codes:
            try:
                diagnostic_result = record(
                    job,
                    stages,
                    11,
                    "gcc-command-diagnostic",
                    build_execution.execute_plan(
                        argument(plan=str(plan_path), phase="preflight", log_dir=job["directories"]["logs"])
                    ),
                )
            except errors.ExecutorError as diagnostic_error:
                diagnostic_result = {
                    "status": "FAILED",
                    "error": {"code": diagnostic_error.code, "evidence": list(diagnostic_error.evidence)},
                }
        injection_state["commandLine"] = {
            "status": "FAILED",
            "plan": str(plan_path),
            "failure": {"code": command_line_error.code, "evidence": list(command_line_error.evidence)},
            "diagnosticPreflight": diagnostic_result,
        }
        record_coverage_injection_evidence(
            job, stages, 11, "coverage-injection-command-line-failure", injection_state
        )
        if command_line_error.code not in fallback_codes:
            raise
        if getattr(args, "fail_fast", False):
            raise

        reference_paths = [
            item["path"] for item in selected_references.get("files", []) if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        fallback_plan_path = Path(job["directories"]["temporary"]) / "build-plan-project-file-patch.json"
        if selection["driver"] == "CMAKE":
            patch_targets = (
                [Path(selection["entry"])] if plan["data"].get("toolInjection") == "MSBUILD"
                else project_backup.cmake_patch_candidates(
                    Path(job["projectRoot"]), Path(selection["entry"]), reference_paths
                )
            )
        else:
            script_paths = [
                item["path"]
                for item in build_evidence.get("buildScripts", [])
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ]
            patch_targets = project_backup.make_patch_candidates(
                Path(job["projectRoot"]),
                Path(selection["entry"]),
                reference_paths,
                script_paths,
                role_real_tools,
            )
        patch = None
        patch_succeeded = False
        try:
            patch = project_backup.ProjectFilePatch.create(job, selection["driver"], patch_targets)
            patch.backup()
        except errors.ExecutorError as patch_backup_error:
            injection_state["projectFilePatch"] = {
                "status": "FAILED",
                "manifest": str(patch.manifest_path) if patch is not None else None,
                "failure": {"code": patch_backup_error.code, "evidence": list(patch_backup_error.evidence)},
            }
            record_coverage_injection_evidence(
                job, stages, 16, "coverage-injection-project-file-backup-failure", injection_state
            )
            raise
        record(
            job,
            stages,
            11,
            "backup-project-file-patch",
            envelopes.envelope(
                "BACKING_UP_PROJECT_FILE_PATCH",
                "SUCCEEDED",
                {"manifest": str(patch.manifest_path), "files": [item["path"] for item in patch.records]},
            ),
        )
        try:
            modified_files = (
                patch.apply_cmake_msbuild({role: str(path) for role, path in wrappers.items()})
                if plan["data"].get("toolInjection") == "MSBUILD"
                else patch.apply_cmake({role: str(path) for role, path in wrappers.items()})
                if selection["driver"] == "CMAKE"
                else patch.apply_make(
                    {role: str(path) for role, path in wrappers.items()},
                    plan["data"]["toolVariables"],
                    role_real_tools,
                )
            )
        except errors.ExecutorError as patch_apply_error:
            injection_state["projectFilePatch"] = {
                "status": "FAILED",
                "manifest": str(patch.manifest_path),
                "failure": {"code": patch_apply_error.code, "evidence": list(patch_apply_error.evidence)},
                "restore": patch.restore(),
            }
            record_coverage_injection_evidence(
                job, stages, 16, "coverage-injection-project-file-patch-failure", injection_state
            )
            raise
        injection_state["projectFilePatch"] = {
            "status": "PATCHED",
            "manifest": str(patch.manifest_path),
            "modifiedFiles": modified_files,
        }
        record_coverage_injection_evidence(
            job, stages, 11, "coverage-injection-project-file-patch", injection_state
        )
        try:
            fallback_artifacts = list(artifacts)
            if selection["driver"] == "CMAKE":
                # The project-file fallback must keep the same normal build
                # directory.  plan_cmake resets its generated CMake state
                # before configuring, so a patched retry cannot reuse the
                # failed command-line coverage cache.
                fallback_build_directory = selection["buildDirectory"]
                fallback_common = {**common, "artifact": fallback_artifacts, "output": str(fallback_plan_path)}
                fallback_plan = gcc_tools.plan_cmake(
                    argument(
                        **fallback_common,
                        cmake=build_tool,
                        source=selection["sourceDirectory"],
                        preserve_source_spelling=False,
                        build=str(fallback_build_directory),
                        msbuild_project_patch=plan["data"].get("toolInjection") == "MSBUILD",
                        generator=selection["generator"],
                        configuration=args.gcc_configuration or selection["configuration"],
                        configure_arg=configure_arguments,
                        target=coverage_targets,
                    )
                )
            else:
                fallback_common = {**common, "artifact": fallback_artifacts, "output": str(fallback_plan_path)}
                fallback_plan = gcc_tools.plan_make(
                    argument(
                        **fallback_common,
                        make=build_tool,
                        makefile=selection["entry"],
                        working_directory=selection["buildDirectory"],
                        clean_target=coverage_clean_target,
                        force_rebuild=coverage_force_rebuild,
                        tool_variable=[
                            f"{role}={name}"
                            for role, name in original_plan["coverageInjection"].get("toolVariables", {}).items()
                        ],
                        allow_native_ar_fallback=ar_probe["fallbackRequired"],
                        target=coverage_targets,
                    )
                )
            fallback_raw = fallback_plan["data"]
            fallback_raw = {key: value for key, value in fallback_raw.items() if key != "integrity"}
            fallback_raw["injectionMethod"] = "PROJECT_FILE_PATCH"
            fallback_raw["projectFilePatchManifest"] = str(patch.manifest_path)
            fallback_raw = fileio.add_integrity(fallback_raw)
            fileio.atomic_write_json(fallback_plan_path, fallback_raw)
            fallback_plan = envelopes.envelope("PLANNING_BUILD", "SUCCEEDED", fallback_raw)
            record(job, stages, 11, f"plan-{selection['driver'].lower()}-project-file-patch", fallback_plan)
            manual_plan = fallback_raw
            build_result = gcc_coverage.execute_gcc_plan(
                job,
                stages,
                record,
                fallback_plan_path,
                fallback_plan["data"]["adapter"],
                fallback_artifacts,
                args.execute,
            )
            injection_state["projectFilePatch"]["status"] = "SUCCEEDED"
            persistent_patch_records = patch.promote_side_by_side()
            injection_state["projectFilePatch"]["persistentBackups"] = persistent_patch_records
            patch_succeeded = True
        except errors.ExecutorError as patch_error:
            injection_state["projectFilePatch"]["status"] = "FAILED"
            injection_state["projectFilePatch"]["failure"] = {
                "code": patch_error.code,
                "evidence": list(patch_error.evidence),
            }
            raise
        finally:
            if patch_succeeded:
                record_coverage_injection_evidence(
                    job, stages, 16, "coverage-injection-project-file-retained", injection_state
                )
            else:
                restore = patch.restore()
                injection_state["projectFilePatch"]["restore"] = restore
                record_coverage_injection_evidence(
                    job, stages, 16, "coverage-injection-project-file-restore", injection_state
                )
    record_coverage_injection_evidence(
        job, stages, 16, "coverage-injection-result", injection_state
    )
    command_names = [name for name in ("configure", "clean", "rebuild", "build")
                     if name in manual_plan["commands"]]
    assets = [
        manual_plan["coverConfig"]
        for _ in [0]
        if manual_plan.get("coverConfig") and pathutil.path_within(manual_plan["coverConfig"], job["jobDirectory"])
    ]
    if manual_plan.get("msbuildProps"):
        assets.append(manual_plan["msbuildProps"])
    return {
        **build_result,
        "buildSucceeded": True,
        "_manualCoverage": {
            "buildSystem": selection["driver"],
            "commands": [manual_plan["commands"][name] for name in command_names],
            "assets": assets,
            "artifacts": list(artifacts),
            "mode": "PROJECT_FILE_PATCH" if persistent_patch_records else "COMMAND_LINE_RECIPE",
            "details": {
                "entry": selection["entry"],
                "buildDirectory": selection["buildDirectory"],
                "targets": coverage_targets,
                "injectionMethod": manual_plan.get("injectionMethod"),
                "existingProjectChanges": persistent_patch_records,
            },
        },
    }


def run(
    job: dict[str, Any],
    discovery: dict[str, Any],
    build_system: str,
    choices: dict[str, Any],
    stages: list[dict[str, Any]],
    args: argparse.Namespace,
    build_evidence: dict[str, Any] | None = None,
    ensure_authentication: Any | None = None,
) -> dict[str, Any]:

    build_evidence, selection, selected_references, build_tool, configure_arguments, original_plan = _prepare_original_plan(job, discovery, build_system, choices, stages, args, build_evidence)
    toolchain, artifacts, wrapper_mapping, original_prefix = _build_and_review_original(job, choices, stages, args, selection, selected_references, build_tool, configure_arguments, original_plan)
    descriptor, wrappers, glue_runtime, role_real_tools, coverage_required_roles = _prepare_agent_and_runtime(job, choices, stages, args, selection, original_plan, toolchain, wrapper_mapping, original_prefix, ensure_authentication)
    return _build_coverage(job, stages, args, selection, original_plan, artifacts, descriptor, wrappers, glue_runtime, role_real_tools, coverage_required_roles, build_tool, configure_arguments, selected_references, build_evidence, option_recovery=choices.get("coverageOptionRecovery"))
