"""Build one common, secret-free summary from workflow evidence."""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any


SCHEMA = "cover-build-summary/v1"
SENSITIVE_ARGUMENT = re.compile(
    r"(?i)(?:password|passwd|token|secret|authorization|api[_-]?key|private[_-]?key|credential)"
)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _stage_documents(stages: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    documents: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for descriptor in stages:
        path = descriptor.get("eventPath") or descriptor.get("path")
        if not isinstance(path, str):
            continue
        document = _load_json(Path(path))
        if document is not None and "logicalStageId" in document:
            document = document.get("result")
        if document is not None:
            documents.append((descriptor, document))
    return documents


def _survey(job: dict[str, Any]) -> dict[str, Any]:
    directories = job.get("directories") if isinstance(job.get("directories"), dict) else {}
    candidates: list[Path] = []
    if directories.get("temporary"):
        candidates.append(Path(directories["temporary"]) / "cover-environment-survey.json")
    if job.get("jobDirectory"):
        candidates.append(Path(job["jobDirectory"]) / "temporary" / "cover-environment-survey.json")
    for candidate in candidates:
        value = _load_json(candidate)
        if value is not None:
            return value
    return {}


def _original_evidence(job: dict[str, Any]) -> dict[str, Any]:
    directories = job.get("directories") if isinstance(job.get("directories"), dict) else {}
    temporary = directories.get("temporary")
    if isinstance(temporary, str):
        value = _load_json(Path(temporary) / "original-build-evidence.json")
        if value is not None:
            return value
    return {}


def _original_plan(documents: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    for descriptor, document in documents:
        if descriptor.get("name") != "execute-original-build":
            continue
        data = document.get("data") if isinstance(document.get("data"), dict) else {}
        plan = data.get("originalBuildPlan")
        if isinstance(plan, dict):
            return plan
    return {}


def _build_system_id(driver: Any, plan: dict[str, Any], documents: list[tuple[dict[str, Any], dict[str, Any]]]) -> str | None:
    normalized = str(driver or plan.get("driver") or "").upper()
    if normalized == "MSBUILD":
        return "VISUAL_STUDIO"
    if normalized == "CMAKE":
        return "CMAKE"
    if normalized == "MAKE":
        return "MAKE"
    if normalized == "STM32CUBEIDE":
        return "STM32CUBEIDE"
    if normalized in {
        "CSHARP_MSBUILD",
        "CSHARP_DOTNET",
        "JAVA_MAVEN",
        "JAVA_GRADLE",
        "JAVA_CLI",
        "VISUAL_STUDIO",
        "STM32CUBEIDE",
    }:
        return normalized
    return normalized or None


def _safe_arguments(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return ["[REDACTED]" if SENSITIVE_ARGUMENT.search(str(value)) else str(value) for value in values]


def _coverage_commands(documents: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...], str, str]] = set()
    for descriptor, document in documents:
        number = descriptor.get("number")
        name = str(descriptor.get("name") or "")
        if not isinstance(number, int) or number < 10:
            continue
        data = document.get("data") if isinstance(document.get("data"), dict) else {}
        for result in data.get("results") or []:
            if not isinstance(result, dict) or not isinstance(result.get("executable"), str):
                continue
            arguments = _safe_arguments(result.get("arguments"))
            working_directory = str(result.get("workingDirectory") or "")
            identity = (result["executable"], tuple(arguments), working_directory, name)
            if identity in seen:
                continue
            seen.add(identity)
            environment = result.get("environmentDelta") if isinstance(result.get("environmentDelta"), dict) else {}
            commands.append(
                {
                    "phase": name,
                    "executable": result["executable"],
                    "arguments": arguments,
                    "workingDirectory": working_directory,
                    "environmentVariableNames": sorted(str(key) for key in environment),
                    "exitCode": result.get("exitCode"),
                    "startedAt": result.get("startedAt"),
                    "finishedAt": result.get("finishedAt"),
                    "durationSeconds": result.get("durationSeconds"),
                }
            )
    return commands


def _glue_code_compiler(documents: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any] | None:
    for descriptor, document in documents:
        if descriptor.get("name") != "resolve-glue-code-compiler":
            continue
        data = document.get("data") if isinstance(document.get("data"), dict) else {}
        if isinstance(data.get("path"), str):
            return {
                key: data[key]
                for key in ("path", "provenance", "mappingPolicy", "configurationMethod", "coverConfig")
                if key in data
            }
    return None


def _msbuild_target(commands: list[dict[str, Any]]) -> str | None:
    for command in commands:
        for argument in command.get("arguments") or []:
            match = re.match(r"(?i)^/(?:t|target):(.+)$", argument)
            if match:
                return match.group(1)
    return None


def _language(documents: list[tuple[dict[str, Any], dict[str, Any]]], build_system: str | None) -> str | None:
    for descriptor, document in documents:
        if descriptor.get("name") != "discover-project":
            continue
        data = document.get("data") if isinstance(document.get("data"), dict) else {}
        languages = data.get("languages")
        if isinstance(languages, list):
            normalized = {str(item).upper() for item in languages}
            if {"C", "CXX"}.issubset(normalized) or {"C", "C++"}.issubset(normalized):
                return "C_AND_CXX"
            if "CXX" in normalized or "C++" in normalized:
                return "CXX"
            if "C" in normalized:
                return "C"
        if isinstance(data.get("language"), str):
            return str(data["language"]).upper()
    if build_system and build_system.startswith("CSHARP"):
        return "CSHARP"
    if build_system and build_system.startswith("JAVA"):
        return "JAVA"
    return None


def _artifact_kind(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    return {
        ".exe": "EXECUTABLE",
        ".a": "STATIC_LIBRARY",
        ".lib": "STATIC_LIBRARY",
        ".so": "SHARED_LIBRARY",
        ".dll": "SHARED_LIBRARY",
        ".dylib": "SHARED_LIBRARY",
        ".jar": "JAR",
        ".war": "WAR",
    }.get(suffix, "OTHER")


def _artifacts(final: dict[str, Any]) -> list[dict[str, Any]]:
    result = final.get("result") if isinstance(final.get("result"), dict) else {}
    verification = result.get("artifactVerification") if isinstance(result.get("artifactVerification"), dict) else {}
    verification_data = verification.get("data") if isinstance(verification.get("data"), dict) else {}
    comparison = result.get("outputPathComparison") if isinstance(result.get("outputPathComparison"), dict) else {}
    if not comparison and isinstance(verification_data.get("outputPathComparison"), dict):
        comparison = verification_data["outputPathComparison"]
    values = verification_data.get("artifacts")
    if not isinstance(values, list):
        values = [{"path": value} for value in result.get("artifacts") or [] if isinstance(value, str)]
    artifacts: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            continue
        path = value["path"]
        artifacts.append(
            {
                "path": path,
                "kind": _artifact_kind(path),
                "fresh": value.get("fresh"),
                "sameAsOriginal": comparison.get("sameAsOriginal"),
                "size": value.get("size"),
                "sha256": value.get("sha256"),
            }
        )
    return artifacts


def _persistent_setup(final: dict[str, Any]) -> dict[str, Any] | None:
    result = final.get("result") if isinstance(final.get("result"), dict) else {}
    descriptor = result.get("persistentSetup") if isinstance(result.get("persistentSetup"), dict) else {}
    path = descriptor.get("path")
    if not isinstance(path, str):
        return None
    state_path = Path(path).resolve()
    state = _load_json(state_path) or {}
    project_root = Path(state.get("projectRoot") or state_path.parent.parent).resolve()

    def absolute_path(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        candidate = Path(value)
        return str((candidate if candidate.is_absolute() else project_root / candidate).resolve())

    commands_path = absolute_path(state.get("commands"))
    commands = _load_json(Path(commands_path)) if commands_path else None
    file_changes: list[dict[str, Any]] = []
    changes_by_path: dict[str, dict[str, Any]] = {}

    def add_file_change(
        value: Any,
        change_type: str,
        purpose: str,
        backup_paths: list[str] | None = None,
    ) -> None:
        normalized_path = absolute_path(value)
        if normalized_path is None:
            return
        identity = normalized_path.replace("/", "\\").casefold()
        normalized_backups = [path for value in backup_paths or [] if (path := absolute_path(value))]
        existing = changes_by_path.get(identity)
        if existing is not None:
            if change_type == "MODIFIED":
                existing["changeType"] = "MODIFIED"
            if purpose not in existing["purposes"]:
                existing["purposes"].append(purpose)
            for backup in normalized_backups:
                if backup not in existing["backupPaths"]:
                    existing["backupPaths"].append(backup)
            return
        record = {
            "path": normalized_path,
            "changeType": change_type,
            "purposes": [purpose],
            "backupPaths": normalized_backups,
        }
        changes_by_path[identity] = record
        file_changes.append(record)

    for change in state.get("projectChanges") or []:
        if not isinstance(change, dict):
            continue
        backups = [
            value
            for key in ("originalBackup", "sideBySideBackup")
            if isinstance((value := change.get(key)), str) and value
        ]
        add_file_change(change.get("path"), "MODIFIED", str(change.get("kind") or "PROJECT_CHANGE"), backups)
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    for configured_project in details.get("configuredProjects") or []:
        add_file_change(configured_project, "MODIFIED", "COVER_TOOLBOX_CONFIGURATION")
    add_file_change(str(state_path), "CREATED_OR_UPDATED", "APPLIED_STATE")
    add_file_change(commands_path, "CREATED_OR_UPDATED", "MANUAL_COMMANDS")
    state_entrypoints = state.get("entrypoints") if isinstance(state.get("entrypoints"), dict) else {}
    descriptor_entrypoints = descriptor.get("entrypoints") if isinstance(descriptor.get("entrypoints"), dict) else {}
    entrypoints = state_entrypoints or descriptor_entrypoints
    for entrypoint in entrypoints.values():
        add_file_change(entrypoint, "CREATED_OR_UPDATED", "MANUAL_ENTRYPOINT")
    for asset in state.get("assets") or []:
        if isinstance(asset, dict):
            add_file_change(asset.get("path"), "CREATED_OR_UPDATED", "PERSISTENT_ASSET")
    return {
        "state": state.get("state") or descriptor.get("state"),
        "path": str(state_path),
        "mode": state.get("mode"),
        "entrypoints": entrypoints,
        "manualCommands": (commands or {}).get("commands") or [],
        "fileChanges": file_changes,
    }


def build_summary(job: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    stages = final.get("stages") if isinstance(final.get("stages"), list) else []
    documents = _stage_documents(stages)
    survey = _survey(job)
    original_evidence = _original_evidence(job)
    decision = survey.get("originalBuildDecision") if isinstance(survey.get("originalBuildDecision"), dict) else {}
    selection = decision.get("selection") if isinstance(decision.get("selection"), dict) else {}
    plan = _original_plan(documents)
    build_system = _build_system_id(selection.get("driver"), plan, documents)
    commands = _coverage_commands(documents)

    targets = list(selection.get("targets") or plan.get("targets") or [])
    configuration = selection.get("configuration") or plan.get("configuration")
    platform = None
    build_targets = list(targets)
    build_action = commands[-1]["phase"] if commands else None
    if build_system in {"VISUAL_STUDIO", "CSHARP_MSBUILD"}:
        for value in targets:
            if isinstance(value, str) and "|" in value:
                mapped_configuration, mapped_platform = value.split("|", 1)
                configuration = configuration or mapped_configuration
                platform = mapped_platform
                break
        explicit_msbuild_target = _msbuild_target(commands)
        build_targets = [explicit_msbuild_target] if explicit_msbuild_target else []
        build_action = explicit_msbuild_target or build_action

    target_frameworks = plan.get("targetFrameworks") if isinstance(plan.get("targetFrameworks"), list) else []
    profiles = plan.get("profiles") if isinstance(plan.get("profiles"), list) else []
    source_sets = plan.get("sourceSets") if isinstance(plan.get("sourceSets"), list) else []
    projects_or_modules = plan.get("projectsOrModules") if isinstance(plan.get("projectsOrModules"), list) else []
    evidence_paths = [str(value) for value in selection.get("evidence") or plan.get("evidence") or []]
    stage_paths = [str(item.get("path")) for item in stages if isinstance(item, dict) and item.get("path")]
    error = final.get("error") if isinstance(final.get("error"), dict) else None
    if error is None and isinstance(final.get("reason"), dict):
        error = final["reason"]
    failure = None
    if final.get("status") not in {"SUCCEEDED", None}:
        failure = {
            "stage": final.get("state"),
            "code": error.get("code") if error else None,
            "message": error.get("message") if error else None,
        }

    language = _language(documents, build_system)
    configured_agent_origin = job.get("agentServerOrigin")
    agent_server_origin = str(configured_agent_origin) if configured_agent_origin else ""
    if not agent_server_origin and not job.get("configuredServerUrl"):
        agent_server_origin = str(job.get("serverOrigin") or "")
    agent_scheme = urllib.parse.urlsplit(agent_server_origin).scheme.casefold()
    if agent_scheme == "http":
        local_agent_transport = "PLAINTEXT_HTTP"
        local_agent_tls_policy = "NOT_APPLICABLE"
    elif agent_scheme == "https":
        local_agent_transport = "HTTPS"
        local_agent_tls_policy = (
            "JOB_SCOPED_PRESENTED_CERTIFICATE_TRUST"
            if job.get("insecureSkipTlsVerify") is True
            else "DEFAULT_JAVA_TLS_VERIFICATION"
        )
    else:
        local_agent_transport = "UNKNOWN"
        local_agent_tls_policy = "UNKNOWN"
    transport_security = {
        "serverOrigin": job.get("serverOrigin"),
        "agentServerOrigin": agent_server_origin or None,
        "tlsCertificateVerification": (
            "DISABLED_BY_SKILL_POLICY" if job.get("insecureSkipTlsVerify") is True else "ENABLED"
        ),
        "localAgentTransport": local_agent_transport,
        "localAgentTlsPolicy": local_agent_tls_policy,
    }
    if job.get("configuredServerUrl") is not None:
        transport_security["configuredServerUrl"] = job["configuredServerUrl"]
    if job.get("openApiServerUrlSource") is not None:
        transport_security["openApiServerUrlSource"] = job["openApiServerUrlSource"]
    if job.get("serverTransportAdaptation") is not None:
        transport_security["serverTransportAdaptation"] = job["serverTransportAdaptation"]
    summary = {
        "schema": SCHEMA,
        "jobId": job.get("jobId"),
        "status": final.get("status"),
        "language": language,
        "transportSecurity": transport_security,
        "buildSystem": {
            "id": build_system,
            "evidenceSource": selection.get("evidenceSource") or plan.get("evidenceSource"),
            "evidencePaths": evidence_paths,
        },
        "target": {
            "entry": selection.get("entry") or plan.get("adapterEntry"),
            "projectsOrModules": projects_or_modules,
            "buildTargets": build_targets,
            "targetSelection": "EXPLICIT" if build_targets else "DEFAULT",
            "buildAction": build_action,
            "configuration": configuration,
            "platform": platform,
            "targetFrameworks": target_frameworks,
            "profiles": profiles,
            "sourceSets": source_sets,
            "targetOs": "STM32" if build_system == "STM32CUBEIDE" else None,
        },
        "coverageBuild": {
            "injectionMethod": plan.get("coverageInjection", {}).get("strategy")
            if isinstance(plan.get("coverageInjection"), dict)
            else None,
            "commands": commands,
            "glueCodeCompiler": _glue_code_compiler(documents),
            "instrumentationProof": (
                final.get("result", {}).get("instrumentationProof")
                if isinstance(final.get("result"), dict)
                else None
            ),
        },
        "actualObservedToolchain": original_evidence.get("actualObservedToolchain"),
        "artifacts": _artifacts(final),
        "persistentSetup": _persistent_setup(final),
        "failure": failure,
        "evidenceFiles": sorted(set(evidence_paths + stage_paths)),
    }
    if job.get("jobDirectory"):
        from cover_build.core.state_store import performance_summary
        summary["performance"] = performance_summary(job)
    final_result = final.get("result") if isinstance(final.get("result"), dict) else {}
    guidance = final_result.get("executionGuidance")
    if language == "JAVA" and isinstance(guidance, dict):
        summary["executionGuidance"] = guidance
    return summary


def attach(job: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    result = final.get("result") if isinstance(final.get("result"), dict) else {}
    result["buildSummary"] = build_summary(job, final)
    final["result"] = result
    return final
