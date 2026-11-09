"""End-to-end Java JACOV coverage workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable

import managed_tools
import project_index

from cover_build.core import state_store
from cover_build.core.job_evidence import with_output_tracking
from cover_build.core.errors import WorkflowWaiting
from cover_build.planning.selection import java_build_entries, java_build_systems
from cover_build.planning import option_recovery
from cover_build.pipelines.managed_common import (
    _absolute_entry,
    _command_spec,
    _executable,
    _managed_plan,
    _project_wrapper,
    _provided_plan,
    _resolve_artifacts,
    _run,
    _select,
)
from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors
from cover_build.pipelines.java import cli, build_runtime

from . import maven
from . import gradle


LOCAL_AGENT_STATIC_FILES = ("bin/agent-config.yaml",)
JAVA_RUNTIME_MODES = ("LOWER", "JAVA_AGENT")


def _server_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError:
        raise errors.ExecutorError("JACOV_CONFIG_INVALID", "JACOV configuration has an invalid server port") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise errors.ExecutorError("JACOV_CONFIG_INVALID", "JACOV configuration has an invalid server URL")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _require_local_agent_installation(install_root: Path) -> dict[str, Any]:
    missing = [str(install_root / relative) for relative in LOCAL_AGENT_STATIC_FILES if not (install_root / relative).is_file()]
    agent_jars = sorted((install_root / "bin").glob("tm-local-agent-*-exec.jar"))
    if not agent_jars:
        missing.append(str(install_root / "bin" / "tm-local-agent-*-exec.jar"))
    if missing:
        raise errors.ExecutorError(
            "AGENT_INSTALLATION_INVALID",
            "Java coverage requires an installed COVER Local Agent and JACOV tool set",
            missing,
        )
    return {"agentRoot": str(install_root), "agentInstalled": True, "localAgentJar": str(agent_jars[-1])}


def _java_prerequisite_stage_data(agent_installation: dict[str, Any], jacov_config: dict[str, Any]) -> dict[str, Any]:
    """Java coverage requires an installed Agent, not a running Agent descriptor."""
    return {
        **agent_installation,
        "jacovConfig": jacov_config,
        "agentStartedForBuild": False,
    }


def _jacov_config(_job: dict[str, Any], authenticate: Callable[[], None] | None = None) -> dict[str, Any]:
    """Validate the JACOV-owned endpoint without coupling it to Open API."""
    configured_path = os.environ.get("COVER_JACOV_CONFIG")
    source = "COVER_JACOV_CONFIG" if configured_path else "USER_HOME"
    key_path = Path(configured_path) if configured_path else Path.home() / "cover_jacov.json"
    if not configured_path and not key_path.exists() and authenticate is not None:
        from cover_build.pipelines.java.jacov_config import create_home_config
        create_home_config(_job, key_path, authenticate)
    if not key_path.is_file():
        raise WorkflowWaiting(
            "JACOV_CONFIG_MISSING",
            "선택한 JACOV 설정 파일이 없습니다. 명시한 COVER_JACOV_CONFIG를 확인하거나 해제하면 실행 중 홈 파일을 생성할 수 있습니다.",
            {},
            decision_owner="USER",
            purpose="SERVER_API",
            evidence=[str(key_path)],
        )
    try:
        value = json.loads(key_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.ExecutorError("JACOV_CONFIG_INVALID", "cover_jacov.json cannot be read as JSON", [str(key_path)]) from exc
    if not isinstance(value, dict) or not isinstance(value.get("url"), str) or not value["url"].strip() or not isinstance(value.get("key"), str) or not value["key"].strip():
        raise errors.ExecutorError("JACOV_CONFIG_INVALID", "cover_jacov.json must contain non-empty url and key fields", [str(key_path)])
    jacov_server_origin = _server_origin(value["url"])
    return {
        "path": str(pathutil.canonical(key_path)),
        "source": source,
        "jacovServerOrigin": jacov_server_origin,
    }


def _jacov_environment(jacov_config: dict[str, Any]) -> dict[str, str]:
    """Pass only the selected config path; never copy its key into the environment."""
    return {"COVER_JACOV_CONFIG": str(jacov_config["path"])}


def _java_major(log: Path, choices: dict[str, Any]) -> int:
    supplied = choices.get("javaMajorVersion")
    if isinstance(supplied, int) and supplied > 0:
        return supplied
    expressions = (
        re.compile(r"COVER_OBS_JAVA_VERSION=(\d+)"),
        re.compile(r"Java version:\s*(\d+)", re.IGNORECASE),
        re.compile(r"(?:openjdk|java) version [\"'](?:1\.)?(\d+)", re.IGNORECASE),
    )
    observed = set()
    for line in processes.iter_output_lines(log):
        for expression in expressions:
            match = expression.search(line)
            if match:
                observed.add(int(match.group(1)))
    if len(observed) == 1:
        return observed.pop()
    raise WorkflowWaiting(
        "JAVA_VERSION_REQUIRED", "The original build did not expose the selected JDK major version",
        {"javaMajorVersion": []}, decision_owner="MODEL", purpose="BUILD_ENVIRONMENT", evidence=[str(log)]
    )

def _java_home(log: Path, choices: dict[str, Any]) -> Path:
    supplied = choices.get("javaHome")
    if isinstance(supplied, str) and supplied:
        return pathutil.canonical(supplied)
    expressions = (
        (re.compile(r"COVER_OBS_JAVA_HOME=(.+)$"), False),
        (re.compile(r"Compiling with toolchain '([^']+)'", re.IGNORECASE), False),
        (re.compile(r"(?:Java|JVM) home:\s*(.+)$", re.IGNORECASE), False),
        (re.compile(r"\bjava\.home\s*[=:]\s*(.+)$", re.IGNORECASE), False),
        (re.compile(
            r"\bruntime:\s*(.+?)(?=,\s*(?:default locale|platform encoding|os name|os version|os arch)\s*:|$)",
            re.IGNORECASE,
        ), True),
    )
    candidates: list[Path] = []
    for line in processes.iter_output_lines(log):
        for expression, require_jdk_tools in expressions:
            match = expression.search(line)
            if not match:
                continue
            candidate = Path(match.group(1).strip().strip("\"'"))
            has_jdk_tools = not require_jdk_tools or (
                any((candidate / "bin" / name).is_file() for name in ("java", "java.exe"))
                and any((candidate / "bin" / name).is_file() for name in ("javac", "javac.exe"))
            )
            if candidate.is_dir() and has_jdk_tools and not any(
                pathutil.same_path(candidate, value) for value in candidates
            ):
                candidates.append(pathutil.canonical(candidate))
    if len(candidates) != 1:
        raise WorkflowWaiting(
            "JAVA_HOME_SELECTION_REQUIRED",
            "The original detailed build did not resolve one actual JDK home",
            {"javaHome": [str(value) for value in candidates]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[str(log)],
        )
    return candidates[0]

def _java_toolchain_observation(log: Path, java_home: Path, major: int) -> dict[str, Any]:
    javac_expression = re.compile(r'((?:[A-Za-z]:\\|/)[^\s"\']*[/\\]javac(?:\.exe)?)', re.IGNORECASE)
    level_expression = re.compile(r'(?:--release|-source|-target)\s+(\d+)', re.IGNORECASE)
    compilers: list[Path] = []
    levels: list[str] = []
    for line in processes.iter_output_lines(log):
        for raw in javac_expression.findall(line):
            if Path(raw).is_file():
                path = pathutil.canonical(raw)
                if not any(pathutil.same_path(path, current) for current in compilers):
                    compilers.append(path)
        levels.extend(level_expression.findall(line))
    return {
        "kind": "JAVA",
        "javaHome": str(java_home),
        "majorVersion": major,
        "compilerMode": "FORKED_JAVAC" if compilers else "IN_PROCESS_OR_DAEMON",
        "compilerExecutables": [str(path) for path in compilers],
        "observedLanguageLevels": sorted(set(levels), key=int),
        "evidence": [str(log)],
    }

def _jacov_artifact(
    install: dict[str, Any], system: str, major: int, selected: str | None
) -> Path:
    prefixes = {
        "MAVEN": "jacov-maven-plugin-",
        "GRADLE": "jacov-gradle-plugin-",
        "COMMAND_LINE_CANDIDATE": "cover-jacov-cli-",
    }
    prefix = prefixes.get(system, "cover-jacov-cli-")
    candidates = [
        item for item in install["binArtifacts"]
        if item.get("exists") and str(item.get("name") or "").startswith(prefix)
    ]
    if not selected:
        compatible = [item for item in candidates if item.get("javaVariant") == major and (
            (system == "GRADLE" and item.get("gradlePlugin", {}).get("minimumJavaMajor", major + 1) <= major)
            or (system == "MAVEN" and item.get("mavenPlugin")))]
        if len(compatible) == 1:
            return pathutil.canonical(compatible[0]["path"])
        raise WorkflowWaiting(
            "JACOV_TOOL_SELECTION_REQUIRED",
            "Select an installed JACOV artifact using the observed JDK/source level and plugin metadata",
            {"jacovTool": [str(item["path"]) for item in candidates]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[f"observedJavaMajor={major}"],
        )
    selected_path = pathutil.canonical(selected)
    record = next(
        (item for item in candidates if pathutil.same_path(str(item["path"]), selected_path)),
        None,
    )
    if record is None:
        raise errors.ExecutorError(
            "JACOV_TOOL_NOT_FOUND",
            "The selected JACOV artifact is not an installed candidate for this build system",
            [str(selected_path), *[str(item["path"]) for item in candidates]],
        )
    minimum = record.get("gradlePlugin", {}).get("minimumJavaMajor") or record.get("javaVariant")
    if major > 0 and minimum and minimum > major:
        raise errors.ExecutorError("JACOV_JDK_INCOMPATIBLE", "The selected JACOV plugin requires a newer JDK", [str(selected_path)])
    return pathutil.canonical(record["path"])


def _postbuild_choices(job: dict[str, Any], choices: dict[str, Any], command: dict[str, Any],
                       install: dict[str, Any], system: str) -> dict[str, Any]:
    unresolved: dict[str, Any] = {}
    found: dict[str, Any] = {}
    actions = {
        "artifacts": lambda: [str(p) for p in _resolve_artifacts(job, choices, command, {".jar", ".war"})],
        "major": lambda: _java_major(Path(command["log"]), choices),
        "javaHome": lambda: str(_java_home(Path(command["log"]), choices)),
    }
    for name, action in actions.items():
        try:
            found[name] = action()
        except WorkflowWaiting as exc:
            unresolved.update(exc.choices)
    try:
        found["tool"] = str(_jacov_artifact(install, system, found.get("major", 0), choices.get("jacovTool")))
    except WorkflowWaiting as exc:
        unresolved.update(exc.choices)
    if system == "COMMAND_LINE_CANDIDATE" and not choices.get("javaCliIntegration"):
        unresolved["javaCliIntegration"] = {}
    try:
        found["buildRuntime"] = build_runtime.validate_plan(choices.get("javaBuildRuntime"), system)
    except WorkflowWaiting as exc:
        unresolved.update(exc.choices)
    if unresolved:
        raise WorkflowWaiting("JAVA_POSTBUILD_DECISION_BUNDLE_REQUIRED",
                              "원본 빌드에서 확정하지 못한 Java 도구와 산출물을 함께 선택한다.",
                              unresolved, decision_owner="MODEL", purpose="BUILD_ENVIRONMENT", evidence=[command["log"]])
    return found


def _check_java_connection(config: dict[str, Any]) -> dict[str, Any]:
    parsed = urlsplit(config["jacovServerOrigin"])
    try:
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=5):
            pass
    except OSError as exc:
        raise errors.ExecutorError("JACOV_NETWORK_UNAVAILABLE",
                                     f"JACOV TCP connection failed ({type(exc).__name__}); clean was not started") from exc
    return {"serverOrigin": config["jacovServerOrigin"], "tcpReachable": True,
            "jacovAuthenticationVerified": False, "tlsVerified": False}


def _recover_missing_artifacts(artifacts: list[Path], spec: dict[str, Any], logs: Path) -> dict[str, Any]:
    missing = [str(p) for p in artifacts if not p.is_file()]
    if not missing:
        return {"recovered": False, "reason": "ARTIFACTS_PRESENT"}
    command = _run(spec, logs / "recovery-original-java.log", "ORIGINAL_ARTIFACT_RECOVERY_FAILED")
    remaining = [str(p) for p in artifacts if not p.is_file()]
    if remaining:
        raise errors.ExecutorError("ORIGINAL_ARTIFACT_RECOVERY_FAILED", "Recovery did not restore every original output", remaining)
    return {"recovered": True, "missingBefore": missing, "command": command}


def _verified_coverage(job: dict[str, Any], stages: list, record: Callable, artifacts: list[Path],
                       coverage_spec: dict[str, Any], original_spec: dict[str, Any],
                       before_recovery: Callable[[], None] | None = None,
                       runtime_plan: dict | None = None) -> dict[str, Any]:
    try:
        coverage = _run(coverage_spec, Path(job["directories"]["logs"]) / "coverage-java.log", "COVERAGE_BUILD_FAILED")
        outputs = artifacts
    except (errors.ExecutorError, KeyboardInterrupt) as error:
        if before_recovery is not None:
            before_recovery()
        try:
            recovery = _recover_missing_artifacts(artifacts, original_spec, Path(job["directories"]["logs"]))
            record(job, stages, 13, "recover-java-artifacts", envelopes.envelope("RECOVERING_JAVA_ARTIFACTS", "SUCCEEDED", recovery))
        except errors.ExecutorError as recovery_error:
            record(job, stages, 13, "recover-java-artifacts", envelopes.envelope("RECOVERING_JAVA_ARTIFACTS", "FAILED", {"errorCode": recovery_error.code}))
            error.add_note(f"Recovery failed: {recovery_error.code}")
            raise error
        if isinstance(error, errors.ExecutorError):
            build_runtime.replan_after_failure(job, runtime_plan, error)
        raise
    return envelopes.envelope("EXECUTING_JAVA_COVERAGE_BUILD", "SUCCEEDED", {
        "results": [coverage], "artifacts": [str(p) for p in outputs],
        "successCriterion": "BUILD_EXIT_CODE",
        "buildInterval": {"startedAt": coverage["startedAt"], "finishedAt": coverage["finishedAt"]},
    })


def _java_runtime_mode(choices: dict[str, Any], execution_arguments: list[str] | None = None) -> str:
    supplied = choices.get("javaRuntimeMode")
    arguments = execution_arguments or []
    safe_arguments = [
        "[REDACTED]"
        if re.search(r"(?i)(?:password|passwd|token|secret|authorization|api[_-]?key)", value)
        else value
        for value in arguments
    ]
    java_agent = any(
        re.search(r"(?i)-javaagent:.*cover-javalib-agent\.jar", value)
        for value in arguments
    )
    lower = any(
        re.search(r"(?i)(?:maven\.jacov\.addDependencyJavaLib|jacov\.addDependencyJavaLib)=true", value)
        or "tm-javalib-lower" in value.casefold()
        for value in arguments
    )
    if java_agent and lower:
        raise errors.ExecutorError(
            "JAVA_RUNTIME_MODE_CONFLICT",
            "Both LOWER and JAVA_AGENT runtime evidence were found",
            safe_arguments,
        )
    if supplied is None:
        if java_agent:
            return "JAVA_AGENT"
        if lower:
            return "LOWER"
        raise WorkflowWaiting(
            "JAVA_RUNTIME_MODE_REQUIRED",
            "Select the runtime only when executing the instrumented Java application",
            {"javaRuntimeMode": list(JAVA_RUNTIME_MODES)},
            decision_owner="MODEL",
            purpose="EXECUTION_RUNTIME",
            evidence=safe_arguments,
        )
    if not isinstance(supplied, str) or supplied.upper() not in JAVA_RUNTIME_MODES:
        raise errors.ExecutorError(
            "JAVA_RUNTIME_MODE_INVALID",
            f"javaRuntimeMode must be one of: {', '.join(JAVA_RUNTIME_MODES)}",
            [str(supplied)],
        )
    selected = supplied.upper()
    if (selected == "LOWER" and java_agent) or (selected == "JAVA_AGENT" and lower):
        raise errors.ExecutorError(
            "JAVA_RUNTIME_MODE_CONFLICT",
            "The selected Java runtime mode conflicts with the execution arguments",
            safe_arguments,
        )
    return selected


def _java_execution_guidance(install_root: Path, runtime_dependency: dict[str, Any] | None) -> dict[str, Any]:
    runtime = pathutil.canonical(install_root / "bin" / "tm-javalib-lower.jar", False)
    generated_agent = pathutil.canonical(
        install_root / "java_agent" / "cover-javalib-agent.jar", False
    )
    metadata = (runtime_dependency or {}).get("metadata") or {}
    artifact = metadata.get("artifactId") or "tm-javalib-lower"
    version = metadata.get("version")
    groups = metadata.get("groupIds") if isinstance(metadata.get("groupIds"), list) else []
    coordinates = [f"{group}:{artifact}:{version}" for group in groups if group and version]
    agent_available = generated_agent.is_file()
    return {
        "selection": "CHOOSE_ONE_AT_EXECUTION",
        "coverageBuildRuntimeInjected": False,
        "decisionRule": {
            "localAgentLower": "Choose only when the target JVM can reach a verified COVER Local Agent Java listener and the lower JAR can be added exactly once.",
            "javaLibAgent": "Choose only when the generated agent is verified for the current server, can be deployed beside the target JVM, and the JVM command can be changed.",
        },
        "localAgentLower": {
            "readiness": "REQUIRES_RUNTIME_VALIDATION",
            "runtimeJar": str(runtime),
            "runtimeJarAvailable": runtime.is_file(),
            "mavenCoordinates": coordinates,
            "repositoryAvailabilityDoesNotEnableRuntime": True,
            "classpathSeparator": os.pathsep,
            "action": "Add tm-javalib-lower.jar exactly once to the application runtime classpath or runtime dependency set, then run the instrumented application with its normal main class and arguments.",
            "mainClassTemplate": {
                "kind": "NON_EXECUTABLE_TEMPLATE",
                "command": f'<existing-java-executable> <existing-jvm-options> -cp "<existing-runtime-classpath>{os.pathsep}{runtime}" <MAIN_CLASS> <application-arguments>',
            },
            "executableJar": {
                "readiness": "REQUIRES_LAUNCHER_SPECIFIC_CONFIGURATION",
                "reason": "A generic java -jar launch does not accept an added application classpath; confirm the launcher, manifest, or alternate main-class launch before adding lower.",
            },
        },
        "javaLibAgent": {
            "readiness": "REQUIRES_RUNTIME_VALIDATION" if agent_available else "REQUIRES_PREPARATION",
            "agentJar": str(generated_agent),
            "agentJarAvailable": agent_available,
            "vmOption": f"-javaagent:{generated_agent}",
            "mainClassTemplate": {
                "kind": "NON_EXECUTABLE_TEMPLATE",
                "command": f'<existing-java-executable> "-javaagent:{generated_agent}" <existing-jvm-options> <MAIN_CLASS> <application-arguments>',
            },
            "executableJarTemplate": {
                "kind": "NON_EXECUTABLE_TEMPLATE",
                "command": f'<existing-java-executable> "-javaagent:{generated_agent}" <existing-jvm-options> -jar <APPLICATION_JAR> <application-arguments>',
            },
            "note": "Use the generated java_agent copy, not the installation bin copy.",
        },
    }



@dataclass(frozen=True, kw_only=True)
class OriginalPlan:
    plan: dict[str, Any]
    system: str
    entry: Path
    executable: Path
    spec: dict[str, Any]
    observer: Path | None


@dataclass(frozen=True, kw_only=True)
class OriginalBuild:
    command: dict[str, Any]
    install_root: Path
    install: dict[str, Any]
    artifacts: list[Path]
    major: int
    java_home: Path
    tool: Path
    runtime_plan: dict[str, Any]
    runtime_info: dict[str, Any] | None
    jacov_config: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class CoverageArguments:
    arguments: list[str]
    cli_patch: tuple | None
    inplace_command: dict[str, Any] | None
    inplace_assets: list[str]


@dataclass(frozen=True, kw_only=True)
class CoverageExecution:
    spec: dict[str, Any]
    inputs: dict[str, Any]
    cli_patch: tuple | None
    maven_install: dict[str, Any] | None
    maven_plugin: dict[str, str] | None
    runtime_dependency: dict[str, Any] | None
    build_runtime_evidence: dict[str, Any]


def _prepare_original_plan(*, job, evidence, choices) -> OriginalPlan:
    observer = None
    data = evidence["java"]
    provided = _provided_plan(job, choices, {"JAVA_MAVEN", "JAVA_GRADLE", "JAVA_CLI"})
    if provided is not None:
        plan = provided
        driver = str(plan["driver"])
        system = {
            "JAVA_MAVEN": "MAVEN",
            "JAVA_GRADLE": "GRADLE",
            "JAVA_CLI": "COMMAND_LINE_CANDIDATE",
        }[driver]
        entry = Path(plan["adapterEntry"])
        executable = Path(plan["buildTool"])
        tasks = list(plan.get("targets") or [])
        spec = dict(plan["commands"]["build"])
    else:
        systems = java_build_systems(data)
        system = _select("managedBuildSystem", systems, choices)
        if system == "MAVEN":
            candidates = java_build_entries(data, system, job["projectRoot"])
            entry = _absolute_entry(Path(job["projectRoot"]), _select("managedBuildEntry", candidates, choices))
            executable = _executable(
                "mvn",
                choices.get("managedBuildTool")
                or str(_project_wrapper(entry, Path(job["projectRoot"]), ("mvnw.cmd", "mvnw")) or "")
                or shutil.which("mvn"),
            )
            driver = "JAVA_MAVEN"
            verbose = ["-V"]
        elif system == "GRADLE":
            candidates = java_build_entries(data, system, job["projectRoot"])
            entry = _absolute_entry(Path(job["projectRoot"]), _select("managedBuildEntry", candidates, choices))
            executable = _executable(
                "gradle",
                choices.get("managedBuildTool")
                or str(_project_wrapper(entry, Path(job["projectRoot"]), ("gradlew.bat", "gradlew")) or "")
                or shutil.which("gradle"),
            )
            driver = "JAVA_GRADLE"
            verbose = ["--info"]
        else:
            raise WorkflowWaiting(
                "JAVA_CLI_PLAN_REQUIRED", "Java CLI builds require a typed original and coverage command",
                {"originalBuildPlan": []},
                decision_owner="MODEL", purpose="ORIGINAL_BUILD_COMMAND"
            )
        tasks = choices.get("managedTasks")
        if not isinstance(tasks, list) or not tasks or not all(isinstance(value, str) and value for value in tasks):
            raise WorkflowWaiting(
                "JAVA_TASKS_REQUIRED", "The customer Java goal/task is not evidenced",
                {"managedTasks": []}, decision_owner="MODEL", purpose="ORIGINAL_BUILD_COMMAND", evidence=[str(entry)]
            )
        original_arguments = [*tasks, *verbose]
        spec = _command_spec(executable, original_arguments, entry.parent)
        plan = _managed_plan(job, driver, executable, entry, spec, None, tasks, [], "JACOV_PLUGIN")
    if system == "GRADLE":
        observer = gradle._gradle_observer(job)
        spec = {**spec, "arguments": [*spec["arguments"], "--init-script", str(observer)]}
        plan = {**plan, "commands": {**plan["commands"], "build": spec}}
    plan = with_output_tracking(plan, choices)
    return OriginalPlan(plan=plan, system=system, entry=entry, executable=executable,
                        spec=spec, observer=observer)



def _build_original_and_prepare_jacov(
    *, job, stages, args, record, finalize_survey, write_original_evidence,
    ensure_authentication, choices, original: OriginalPlan,
) -> OriginalBuild:
    plan, system = original.plan, original.system
    executable, spec = original.executable, original.spec
    driver = str(plan["driver"])
    record(job, stages, 4, "resolve-original-build-command", envelopes.envelope("RESOLVING_ORIGINAL_BUILD_COMMAND", "SUCCEEDED", {"plan": plan}))
    finalize_survey(job, stages, plan)
    if not args.execute:
        raise WorkflowWaiting("ORIGINAL_BUILD_EXECUTION_REQUIRED", "The original Java build must run", {"execute": True}, decision_owner="USER", purpose="MUTATION_PERMISSION")
    cli.ensure_sources_restored(job)
    original = state_store.run_checkpointed(
        job, stages, record, 5, "execute-original-build",
        {"plan": plan, "runtimeDigest": state_store.input_digest({name: os.environ.get(name) for name in
            ("JAVA_HOME", "PATH", "GRADLE_USER_HOME", "MAVEN_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS")}),
         "buildToolSha256": pathutil.sha256_file(executable),
         "projectIndexFingerprint": project_index.input_fingerprint(fileio.load_json(Path(job["directories"]["temporary"]) / "project-index.json"))},
        lambda: envelopes.envelope(
            "EXECUTING_ORIGINAL_BUILD", "SUCCEEDED",
            {"driver": driver, "originalBuildPlan": plan, "command": _run(spec, Path(job["directories"]["logs"]) / "original-java.log", "ORIGINAL_BUILD_FAILED")},
        ),
    )
    command = original["data"]["command"]
    install_value = choices.get("agentInstallPath") or os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    if not install_value:
        raise WorkflowWaiting("AGENT_INSTALLATION_REQUIRED", "CODESCROLL_TM_INSTALL_PATH is required",
                              {"agentInstallPath": []}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION")
    install_root = pathutil.canonical(install_value)
    agent_installation = _require_local_agent_installation(install_root)
    install_inputs = {str(p): pathutil.sha256_file(p) for p in (install_root / "bin").glob("*jacov*") if p.is_file()}
    install = state_store.run_checkpointed(job, stages, record, 6, "inspect-jacov-install", install_inputs,
        lambda: envelopes.envelope("INSPECTING_JACOV_INSTALL", "SUCCEEDED", managed_tools.inspect_jacov_install(install_root)))["data"]
    observed = state_store.run_checkpointed(job, stages, record, 6, "resolve-java-original-evidence",
        {"logHash": pathutil.sha256_file(Path(command["log"])), "install": install_inputs,
         "system": system,
         "selections": {key: choices.get(key) for key in ("expectedArtifacts", "javaHome", "javaMajorVersion", "jacovTool")},
         "cliIntegration": choices.get("javaCliIntegration") if system == "COMMAND_LINE_CANDIDATE" else None,
         "buildRuntime": choices.get("javaBuildRuntime")},
        lambda: envelopes.envelope("RESOLVING_JAVA_ORIGINAL_EVIDENCE", "SUCCEEDED", _postbuild_choices(job, choices, command, install, system)))["data"]
    artifacts = [Path(p) for p in observed["artifacts"]]
    major, java_home, tool = observed["major"], Path(observed["javaHome"]), Path(observed["tool"])
    runtime_plan = observed["buildRuntime"]
    build_runtime.check_previous_failure(job, runtime_plan)
    runtime_info = build_runtime.runtime_metadata(install_root, runtime_plan)
    if not java_home.is_dir() or not any((java_home / "bin" / name).is_file() for name in ("javac", "javac.exe")):
        raise errors.ExecutorError("JAVA_HOME_INVALID", "The observed Java home does not contain a JDK compiler", [str(java_home)])
    toolchain_observation = _java_toolchain_observation(Path(command["log"]), java_home, major)
    write_original_evidence(
        job,
        stages,
        envelopes.envelope(
            "CAPTURING_ORIGINAL_BUILD_EVIDENCE",
            "SUCCEEDED",
            {
                "driver": driver,
                "originalBuildPlan": plan,
                "commands": {"build": command},
                "toolchain": {
                    "family": "JAVA",
                    "tools": {"javaHome": str(java_home), "majorVersion": str(major)},
                    "evidence": {"javaHome": [command["log"]]},
                    "observation": toolchain_observation,
                },
                "artifacts": [{"path": str(path)} for path in artifacts],
            },
        ),
    )
    jacov_config = _jacov_config(job, ensure_authentication)
    record(
        job,
        stages,
        6,
        "verify-java-agent-and-jacov-config",
        envelopes.envelope(
            "VERIFYING_JAVA_BUILD_PREREQUISITES",
            "SUCCEEDED",
            _java_prerequisite_stage_data(agent_installation, jacov_config),
        ),
    )
    return OriginalBuild(
        command=command, install_root=install_root, install=install, artifacts=artifacts,
        major=major, java_home=java_home, tool=tool, runtime_plan=runtime_plan,
        runtime_info=runtime_info, jacov_config=jacov_config,
    )



def _customer_arguments(system, arguments, *, job, plugin=None):
    """Remove only JACOV's injection, not customer goals/profiles/test options."""
    result = []
    index = 0
    setup = (f"{plugin['groupId']}:{plugin['artifactId']}:{plugin['version']}:setup"
             if plugin else None)
    while index < len(arguments):
        value = arguments[index]
        if system == "MAVEN" and (value == setup or value.startswith("-Dmaven.jacov.")):
            index += 1
            continue
        if system == "GRADLE":
            if value.startswith("-Djacov."):
                index += 1
                continue
            path = None
            width = 1
            if value in {"--init-script", "-I"} and index + 1 < len(arguments):
                path, width = arguments[index + 1], 2
            elif value.startswith("--init-script="):
                path = value.split("=", 1)[1]
            if path and pathutil.same_path(path, Path(job["directories"]["temporary"]) / "jacov-gradle-init.gradle"):
                index += width
                continue
        result.append(value)
        index += 1
    return result


def _validate_additional_options(system, arguments):
    """Consume option operands without mistaking their values for new tasks."""
    value_options = {
        "MAVEN": {"-T", "--threads", "-D", "--define", "-s", "--settings", "-gs", "--global-settings",
                  "-t", "--toolchains", "-gt", "--global-toolchains", "-l", "--log-file"},
        "GRADLE": {"--max-workers", "--priority", "--console", "--warning-mode", "-D", "--system-prop",
                   "-P", "--project-prop", "-I", "--init-script", "-g", "--gradle-user-home"},
    }[system]
    index = 0
    while index < len(arguments):
        value = arguments[index]
        name = value.split("=", 1)[0]
        excluded = (re.match(r"^(?:-x|--exclude-task(?:=|$))", value) if system == "GRADLE"
                    else re.match(r"^(?:-pl|-am|-rf|--projects(?:=|$)|--resume-from(?:=|$)|--also-make)", value))
        if not value.startswith("-") or value == "--" or excluded:
            raise errors.ExecutorError("ORIGINAL_BUILD_ARGUMENTS_CHANGED", "Coverage options may not add goals or exclude tasks/modules")
        property_value = value
        if name in {"--define", "--system-prop", "--project-prop"} and "=" in value:
            property_value = value.split("=", 1)[1]
        if value in value_options:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                raise errors.ExecutorError("INVALID_COVERAGE_OPTION", f"Missing operand for {value}")
            property_value = arguments[index + 1] if value in {"-D", "--define", "--system-prop", "-P", "--project-prop"} else ""
            index += 1
        if re.search(r"(?i)(skip|ignoreFailures|testFailureIgnore)", property_value.split("=", 1)[0]):
            raise errors.ExecutorError("ORIGINAL_BUILD_ARGUMENTS_CHANGED", "Coverage options may not suppress tests or failures")
        index += 1


def _prepare_coverage_arguments(
    *, job, choices, original: OriginalPlan, observed: OriginalBuild, verified_reuse=None,
) -> CoverageArguments:
    system, spec, observer = original.system, original.spec, original.observer
    tool, java_home = observed.tool, observed.java_home
    runtime_plan, runtime_info = observed.runtime_plan, observed.runtime_info
    install, command = observed.install, observed.command
    runtime_build_spec = build_runtime.apply_java_command(runtime_plan, spec, runtime_info)
    coverage_arguments = choices.get("javaCoverageArguments")
    cli_patch = None
    inplace_command = None
    inplace_assets = []
    if system == "COMMAND_LINE_CANDIDATE":
        # Keep the original customer command; the reviewed build-file edits
        # connect source instrumentation to that command's compiler inputs.
        integration = choices.get("javaCliIntegration")
        if isinstance(integration, dict) and integration.get("mode") == "IN_PLACE":
            inplace_command, inplace_assets = cli.prepare_in_place(job, integration, tool, java_home, runtime_build_spec)
        elif runtime_plan.get("application") != "BUILD_FILES":
            cli_patch = cli.prepare(job, integration, tool)
        coverage_arguments = list(runtime_build_spec["arguments"])
    if coverage_arguments is None and system == "GRADLE":
        init_script = gradle._gradle_coverage_script(job, tool)
        coverage_arguments = [*gradle._without_observer(spec["arguments"], observer), "--init-script", str(init_script)]
    elif coverage_arguments is None and system == "MAVEN":
        plugin = maven._maven_plugin_record(install, tool)
        coverage_arguments = maven._maven_coverage_arguments(spec["arguments"], plugin)
    if not isinstance(coverage_arguments, list) or (not coverage_arguments and system != "COMMAND_LINE_CANDIDATE") or not all(isinstance(value, str) for value in coverage_arguments):
        raise WorkflowWaiting(
            "JAVA_COVERAGE_ARGUMENTS_REQUIRED",
            "The installed JACOV plugin descriptor and customer tasks must be combined into typed coverage arguments",
            {"javaCoverageArguments": []},
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
            evidence=[str(tool), command["log"]],
        )
    forbidden = re.compile(r"(?i)(?:project\.build\.directory|outputDirectory|buildDir|destinationDirectory)")
    if any(forbidden.search(value) for value in coverage_arguments):
        raise errors.ExecutorError("COVERAGE_OUTPUT_PATH_CHANGED", "Java coverage arguments may not redirect build outputs")
    if system in {"GRADLE", "MAVEN"}:
        plugin = maven._maven_plugin_record(install, tool) if system == "MAVEN" else None
        baseline = gradle._without_observer(spec["arguments"], observer) if system == "GRADLE" else spec["arguments"]
        additions = option_recovery.additional_arguments(
            _customer_arguments(system, baseline, job=job, plugin=plugin),
            _customer_arguments(system, coverage_arguments, job=job, plugin=plugin),
        )
        _validate_additional_options(system, additions)
        option_recovery.require_failure(
            job, choices.get("coverageOptionRecovery"), additions,
            phases={"coverage-java"}, decision_choices={"javaCoverageArguments": []},
            verified_reuse=verified_reuse,
            coverage_arguments=coverage_arguments,
        )
    return CoverageArguments(arguments=coverage_arguments, cli_patch=cli_patch,
                             inplace_command=inplace_command, inplace_assets=inplace_assets)



def _prepare_coverage_execution(
    *, job, stages, record, ensure_authentication, choices,
    original: OriginalPlan, observed: OriginalBuild, arguments: CoverageArguments,
) -> CoverageExecution:
    system, spec = original.system, original.spec
    executable, entry = original.executable, original.entry
    jacov_config, install, tool = observed.jacov_config, observed.install, observed.tool
    install_root, java_home = observed.install_root, observed.java_home
    runtime_plan, runtime_info, artifacts = observed.runtime_plan, observed.runtime_info, observed.artifacts
    cli_patch, coverage_arguments = arguments.cli_patch, arguments.arguments
    inplace_command = arguments.inplace_command
    # Authentication is process-local. A saved successful stage is not a credential.
    ensure_authentication()
    network = _check_java_connection(jacov_config)
    record(job, stages, 7, "java-connection-check", envelopes.envelope("CHECKING_JAVA_CONNECTION", "SUCCEEDED", network))
    maven_install: dict[str, Any] | None = None
    maven_plugin: dict[str, str] | None = None
    runtime_dependency: dict[str, Any] | None = None
    coverage_effective_arguments = coverage_arguments
    if system == "MAVEN":
        plugin = maven._maven_plugin_record(install, tool)
        maven_plugin = plugin
        if not maven._maven_plugin_coordinate_is_used(coverage_arguments, plugin):
            raise errors.ExecutorError(
                "JACOV_MAVEN_COORDINATE_UNRESOLVED",
                "Coverage arguments do not reference the selected JACOV Maven plugin coordinate",
                [str(tool), *coverage_arguments],
            )
        original_arguments = list(spec.get("arguments") or [])
        repository = maven._maven_local_repository(job, [*original_arguments, *coverage_arguments])
        install_context = maven._maven_install_context(original_arguments)
        install_executable = maven._maven_install_executable(install_root, executable)
        install_spec = maven._maven_plugin_install_spec(install_executable, entry, repository, tool, plugin, install_context)
        plugin_destination = repository / plugin["groupId"].replace(".", "/") / plugin["artifactId"] / plugin["version"] / f"{plugin['artifactId']}-{plugin['version']}.jar"
        install_command = maven._maven_install_once(job, stages, record, "install-jacov-maven-artifact", install_spec, tool, plugin_destination)
        runtime = maven._require_javalib_lower(install_root)
        runtime_metadata = maven._maven_javalib_metadata(runtime)
        runtime_commands = [
            maven._maven_install_once(
                job, stages, record, f"install-tm-javalib-lower-{index}", runtime_spec, runtime,
                repository / runtime_metadata["groupIds"][index - 1].replace(".", "/") / runtime_metadata["artifactId"] / runtime_metadata["version"] / f"{runtime_metadata['artifactId']}-{runtime_metadata['version']}.jar",
            )
            for index, runtime_spec in enumerate(
                maven._maven_javalib_install_specs(install_executable, entry, repository, runtime, runtime_metadata, install_context),
                start=1,
            )
        ]
        runtime_dependency = {
            "mode": "MAVEN_COVERAGE_INSTRUMENTATION",
            "runtimeSelection": "DEFERRED_TO_EXECUTION",
            "addDependencyJavaLib": build_runtime.uses_plugin_dependency(runtime_plan),
            "runtimeJar": {
                "path": str(runtime),
                "sha256": pathutil.sha256_file(runtime),
                "size": runtime.stat().st_size,
            },
            "metadata": runtime_metadata,
            "installCommands": runtime_commands,
        }
        maven_install = {
            "repository": str(repository),
            "installer": str(install_executable),
            "plugin": plugin,
            "pluginJar": {"path": str(tool), "sha256": pathutil.sha256_file(tool)},
            "pluginPom": None,
            "command": install_command,
            "runtimeDependency": runtime_dependency,
        }
        record(job, stages, 12, "install-jacov-maven-plugin", envelopes.envelope("INSTALLING_JACOV_MAVEN_PLUGIN", "SUCCEEDED", maven_install))
        coverage_effective_arguments = maven._maven_repository_argument(
            maven._maven_javalib_argument(coverage_arguments, build_runtime.uses_plugin_dependency(runtime_plan)), repository
        )
    elif system == "GRADLE":
        supplied_init = gradle._gradle_init_script(coverage_arguments)
        if supplied_init.is_file() and pathutil.path_within(supplied_init, job["directories"]["temporary"]):
            supplied_init.write_text(gradle._replace_gradle_jar(supplied_init.read_text(encoding="utf-8-sig"), tool), encoding="utf-8")
        runtime_dependency = gradle._prepare_gradle_runtime(coverage_arguments, job, runtime_plan, runtime_info)
        record(
            job,
            stages,
            12,
            "prepare-jacov-gradle-runtime",
            envelopes.envelope("PREPARING_JACOV_GRADLE_RUNTIME", "SUCCEEDED", runtime_dependency),
        )
    if runtime_plan.get("application") == "BUILD_FILES":
        cli_patch = build_runtime.prepare_build_files(
            job, runtime_plan, runtime_info,
            choices.get("javaCliIntegration") if system == "COMMAND_LINE_CANDIDATE" else None, tool)
    build_runtime.check_runtime_arguments(runtime_plan, coverage_arguments)
    coverage_environment = {**spec.get("environmentDelta", {}), **_jacov_environment(jacov_config)}
    coverage_effective_arguments, coverage_environment = build_runtime.apply(
        runtime_plan, system, coverage_effective_arguments, coverage_environment, runtime_info,
        gradle._gradle_init_script(coverage_arguments) if system == "GRADLE" else None)
    build_runtime_evidence = {"plan": runtime_plan, "runtime": runtime_info,
                              "agent": runtime_info if runtime_plan["mode"] == "JAVA_AGENT" else None,
                              "runtimeInjected": runtime_info is not None and runtime_plan.get("application") != "EXISTING",
                              "existingRuntimeReused": runtime_plan.get("application") == "EXISTING",
                              "separateMeasurementVerification": False}
    record(job, stages, 12, "prepare-java-build-runtime",
           envelopes.envelope("PREPARING_JAVA_BUILD_RUNTIME", "SUCCEEDED", build_runtime_evidence))
    coverage_spec = {**spec, "arguments": coverage_effective_arguments,
                     "environmentDelta": coverage_environment, "coverToolEnvironment": True}
    if inplace_command is not None:
        coverage_spec = {**inplace_command, "environmentDelta": coverage_environment, "coverToolEnvironment": True}
    coverage_inputs = {"command": coverage_spec, "artifacts": [str(p) for p in artifacts],
                       "sourceFingerprint": project_index.input_fingerprint(fileio.load_json(Path(job["directories"]["temporary"]) / "project-index.json")),
                       "jar": pathutil.sha256_file(tool), "jdk": str(java_home),
                       "buildRuntime": build_runtime_evidence,
                       "configDigest": pathutil.sha256_file(Path(jacov_config["path"])),
                       "initHash": pathutil.sha256_file(gradle._gradle_init_script(coverage_arguments)) if system == "GRADLE" else None}
    return CoverageExecution(
        spec=coverage_spec, inputs=coverage_inputs, cli_patch=cli_patch,
        maven_install=maven_install, maven_plugin=maven_plugin,
        runtime_dependency=runtime_dependency, build_runtime_evidence=build_runtime_evidence,
    )



def _execute_coverage(
    *, job, stages, record, original: OriginalPlan, observed: OriginalBuild,
    arguments: CoverageArguments, execution: CoverageExecution,
):
    artifacts, spec, runtime_plan = observed.artifacts, original.spec, observed.runtime_plan
    coverage_spec, coverage_inputs, cli_patch = execution.spec, execution.inputs, execution.cli_patch
    inplace_command = arguments.inplace_command
    cli_changes = []
    if inplace_command is not None and cli_patch is not None:
        def execute_inplace_with_runtime(restore):
            def restore_all():
                restore()
                cli.ensure_sources_restored(job)
            return _verified_coverage(job, stages, record, artifacts, coverage_spec, spec,
                                      before_recovery=restore_all, runtime_plan=runtime_plan)
        outcome, cli_changes = cli.execute(*cli_patch, execute_inplace_with_runtime)
        cli.ensure_sources_restored(job)
        outcome["data"]["sourceRestoration"] = {"mode": "IN_PLACE", "verified": True}
        record(job, stages, 13, "java-coverage-build", outcome)
        verified = outcome["data"]
    elif inplace_command is not None:
        outcome = _verified_coverage(job, stages, record, artifacts, coverage_spec, spec,
                                     before_recovery=lambda: cli.ensure_sources_restored(job), runtime_plan=runtime_plan)
        cli.ensure_sources_restored(job)
        outcome["data"]["sourceRestoration"] = {"mode": "IN_PLACE", "verified": True}
        record(job, stages, 13, "java-coverage-build", outcome)
        verified = outcome["data"]
    elif cli_patch is not None:
        # Never reuse proof for a different edited customer build script.
        outcome, cli_changes = cli.execute(*cli_patch, lambda restore:
            _verified_coverage(job, stages, record, artifacts, coverage_spec, spec, before_recovery=restore, runtime_plan=runtime_plan))
        record(job, stages, 13, "java-coverage-build", outcome)
        verified = outcome["data"]
    else:
        verified = state_store.run_checkpointed(job, stages, record, 13, "java-coverage-build", coverage_inputs,
            # Rerun on resume; do not inspect outputs or reuse a prior build result.
            lambda: _verified_coverage(job, stages, record, artifacts, coverage_spec, spec, runtime_plan=runtime_plan), validate=lambda _: False)["data"]
    return verified, cli_changes


def run_java(
    job: dict[str, Any], evidence: dict[str, Any], choices: dict[str, Any], stages: list[dict[str, Any]],
    args: Any, record: Callable[..., dict[str, Any]], ensure_authentication: Callable[[], None],
    finalize_survey: Callable[..., dict[str, Any]],
    write_original_evidence: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    original = _prepare_original_plan(job=job, evidence=evidence, choices=choices)
    observed = _build_original_and_prepare_jacov(
        job=job, stages=stages, args=args, record=record, finalize_survey=finalize_survey,
        write_original_evidence=write_original_evidence,
        ensure_authentication=ensure_authentication, choices=choices, original=original,
    )
    arguments = _prepare_coverage_arguments(
        job=job, choices=choices, original=original, observed=observed,
        verified_reuse=getattr(args, "verified_coverage_options", None),
    )
    execution = _prepare_coverage_execution(
        job=job, stages=stages, record=record, ensure_authentication=ensure_authentication,
        choices=choices, original=original, observed=observed, arguments=arguments,
    )
    verified, cli_changes = _execute_coverage(
        job=job, stages=stages, record=record, original=original, observed=observed,
        arguments=arguments, execution=execution,
    )
    system, entry = original.system, original.entry
    tool, install_root = observed.tool, observed.install_root
    major, java_home = observed.major, observed.java_home
    runtime_plan, runtime_info = observed.runtime_plan, observed.runtime_info
    coverage_spec = execution.spec
    maven_install, maven_plugin = execution.maven_install, execution.maven_plugin
    runtime_dependency, build_runtime_evidence = execution.runtime_dependency, execution.build_runtime_evidence
    inplace_assets, inplace_command = arguments.inplace_assets, arguments.inplace_command
    coverage_artifacts = [Path(p) for p in verified["artifacts"]]
    build_result = {
        **verified,
        "jacovTool": str(tool),
        "mavenPluginInstall": maven_install,
        "runtimeDependency": runtime_dependency,
        "buildRuntime": build_runtime_evidence,
    }
    execution_guidance = _java_execution_guidance(install_root, runtime_dependency)
    execution_guidance["coverageBuildRuntimeInjected"] = build_runtime_evidence["runtimeInjected"]
    execution_guidance["buildRuntime"] = build_runtime_evidence
    build_result["executionGuidance"] = execution_guidance
    persistent_assets = (
        [runtime_dependency["initScript"], str(tool)]
        if system == "GRADLE" and isinstance(runtime_dependency, dict)
        else []
    )
    persistent_assets.extend(inplace_assets)
    return {
        **build_result,
        "buildSucceeded": True,
        "_manualCoverage": {
            "buildSystem": f"JAVA_{system}",
            "commands": [coverage_spec],
            "assets": persistent_assets,
            "artifacts": [str(path) for path in coverage_artifacts],
            "mode": "MANUAL_JACOV_COMMAND",
            "details": {
                "entry": str(entry),
                "existingProjectChanges": cli_changes,
                "cliSourceMode": ("IN_PLACE" if inplace_command is not None else "SEPARATE_OUTPUT") if system == "COMMAND_LINE_CANDIDATE" else None,
                "projectRoot": job["projectRoot"],
                "javaHome": str(java_home),
                "javaMajorVersion": major,
                "jacovTool": str(tool),
                "runtimeSelection": runtime_plan["mode"] + "_FOR_BUILD_EXECUTION" if runtime_info else "DEFERRED_TO_EXECUTION",
                "buildRuntime": build_runtime_evidence,
                "executionGuidance": execution_guidance,
                "repository": str(maven_install["repository"]) if maven_install else None,
                "mavenPlugin": maven_plugin,
            },
        },
    }


run = run_java
