"""End-to-end Java JACOV coverage workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
import socket
from urllib.parse import urlsplit
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import managed_tools
import project_index

from cover_build.core import state_store
from cover_build.core.errors import WorkflowWaiting
from cover_build.planning.selection import java_build_entries
from cover_build.pipelines.managed_common import (
    _absolute_entry,
    _command_spec,
    _coverage_artifacts,
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


LOCAL_AGENT_STATIC_FILES = ("bin/agent-config.yaml",)
MAVEN_JAVALIB_LEGACY_GROUP_ID = "com.codescroll.tm"
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


GRADLE_OBSERVATION = '''
allprojects {
    tasks.withType(org.gradle.api.tasks.compile.JavaCompile).configureEach {
        doFirst {
            def compiler = javaCompiler.get()
            println("COVER_OBS_JAVA_HOME=" + compiler.metadata.installationPath.asFile.absolutePath)
            println("COVER_OBS_JAVA_VERSION=" + compiler.metadata.languageVersion.asInt())
        }
    }
    tasks.withType(org.gradle.api.tasks.bundling.AbstractArchiveTask).configureEach {
        doLast { println("COVER_OBS_ARTIFACT=" + archiveFile.get().asFile.absolutePath) }
    }
}
'''


def _gradle_observer(job: dict[str, Any]) -> Path:
    path = Path(job["directories"]["temporary"]) / "observe-gradle.gradle"
    if not path.is_file() or path.read_text(encoding="utf-8") != GRADLE_OBSERVATION:
        path.write_text(GRADLE_OBSERVATION, encoding="utf-8")
    return path


def _gradle_coverage_script(job: dict[str, Any], tool: Path) -> Path:
    source = tool.parent / "jacov-gradle-init.gradle"
    if not source.is_file():
        raise errors.ExecutorError("JACOV_GRADLE_INIT_SCRIPT_INVALID", "Installed Gradle init script is missing", [str(source)])
    target = Path(job["directories"]["temporary"]) / "jacov-gradle-init.gradle"
    contents = source.read_text(encoding="utf-8-sig")
    contents = _replace_gradle_jar(contents, tool)
    target.write_text(contents + "\n" + GRADLE_OBSERVATION, encoding="utf-8")
    return target


def _replace_gradle_jar(contents: str, tool: Path) -> str:
    escaped = str(tool).replace("\\", "/").replace("'", "\\'")
    pattern = r'''classpath\s+files\(\s*["'][^"'\r\n]*jacov-gradle-plugin[^"'\r\n]*\.jar["']\s*\)'''
    updated, count = re.subn(pattern, lambda _: f"classpath files('{escaped}')", contents)
    if count != 1:
        raise errors.ExecutorError("JACOV_GRADLE_INIT_SCRIPT_INVALID", "Expected exactly one JACOV plugin classpath in init script")
    return updated


def _without_observer(arguments: list[str], observer: Path) -> list[str]:
    result = list(arguments)
    for i in range(len(result) - 1):
        if result[i] == "--init-script" and result[i + 1] == str(observer):
            return result[:i] + result[i + 2:]
    return result


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
    if system == "COMMAND_LINE_CANDIDATE" and not choices.get("javaCoverageArguments"):
        unresolved["javaCoverageArguments"] = []
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
                       coverage_spec: dict[str, Any], original_spec: dict[str, Any]) -> dict[str, Any]:
    before = Path(job["directories"]["temporary"]) / "java-artifacts-before.json"
    after = Path(job["directories"]["temporary"]) / "java-artifacts-after.json"
    processes.snapshot_artifacts(SimpleNamespace(artifact=[str(p) for p in artifacts], output=str(before)))
    try:
        coverage = _run(coverage_spec, Path(job["directories"]["logs"]) / "coverage-java.log", "COVERAGE_BUILD_FAILED")
        proof = _java_instrumentation_proof(Path(coverage["log"]))
        outputs = _coverage_artifacts(job, coverage, {".jar", ".war"}, artifacts)
        processes.snapshot_artifacts(SimpleNamespace(artifact=[str(p) for p in artifacts], output=str(after)))
        verification = processes.verify_artifacts(SimpleNamespace(before=str(before), after=str(after), output=None))
        comparison = pathutil.require_same_output_paths(artifacts, outputs)
        verification["data"]["outputPathComparison"] = comparison
    except (errors.ExecutorError, KeyboardInterrupt) as error:
        try:
            recovery = _recover_missing_artifacts(artifacts, original_spec, Path(job["directories"]["logs"]))
            record(job, stages, 13, "recover-java-artifacts", envelopes.envelope("RECOVERING_JAVA_ARTIFACTS", "SUCCEEDED", recovery))
        except errors.ExecutorError as recovery_error:
            record(job, stages, 13, "recover-java-artifacts", envelopes.envelope("RECOVERING_JAVA_ARTIFACTS", "FAILED", {"errorCode": recovery_error.code}))
            error.add_note(f"Recovery failed: {recovery_error.code}")
        raise
    return envelopes.envelope("EXECUTING_JAVA_COVERAGE_BUILD", "SUCCEEDED", {
        "results": [coverage], "artifacts": [str(p) for p in outputs],
        "outputHashes": {str(p): pathutil.sha256_file(p) for p in outputs},
        "artifactVerification": verification, "outputPathComparison": comparison,
        "instrumentationProof": proof,
        "buildInterval": {"startedAt": coverage["startedAt"], "finishedAt": coverage["finishedAt"]},
    })


def _coverage_outputs_unchanged(result: dict[str, Any]) -> bool:
    hashes = result.get("data", {}).get("outputHashes") or {}
    return bool(hashes) and all(Path(p).is_file() and pathutil.sha256_file(Path(p)) == sha for p, sha in hashes.items())


def _maven_install_once(job: dict[str, Any], stages: list, record: Callable, name: str,
                        spec: dict[str, Any], source: Path, installed: Path) -> dict[str, Any]:
    source_hash = pathutil.sha256_file(source)
    def action():
        command = _run(spec, Path(job["directories"]["logs"]) / f"{name}.log", "JACOV_MAVEN_PLUGIN_INSTALL_FAILED")
        if not installed.is_file() or pathutil.sha256_file(installed) != source_hash:
            raise errors.ExecutorError("JACOV_MAVEN_PLUGIN_INSTALL_FAILED", "Maven local repository does not contain the selected JAR", [str(installed)])
        return envelopes.envelope("INSTALLING_MAVEN_ARTIFACT", "SUCCEEDED", command)
    return state_store.run_checkpointed(job, stages, record, 12, name,
        {"command": spec, "jarSha256": source_hash, "destination": str(installed)}, action,
        validate=lambda _: installed.is_file() and pathutil.sha256_file(installed) == source_hash)["data"]


def _maven_plugin_record(install: dict[str, Any], tool: Path) -> dict[str, str]:
    record = next(
        (item for item in install["binArtifacts"] if item.get("exists") and pathutil.same_path(str(item["path"]), tool)),
        None,
    )
    plugin = record.get("mavenPlugin") if isinstance(record, dict) else None
    required = ("groupId", "artifactId", "version", "pomPath")
    if not isinstance(plugin, dict) or not all(isinstance(plugin.get(key), str) and plugin[key] for key in required):
        raise errors.ExecutorError(
            "JACOV_MAVEN_PLUGIN_METADATA_INVALID",
            "The selected JACOV Maven plugin has no complete embedded Maven coordinate and POM",
            [str(tool)],
        )
    return {key: str(plugin[key]) for key in required}


def _extract_maven_plugin_pom(tool: Path, plugin: dict[str, str], temporary: Path) -> dict[str, Any]:
    target = temporary / "jacov-maven-plugin.pom.xml"
    try:
        with zipfile.ZipFile(tool) as archive:
            contents = archive.read(plugin["pomPath"])
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise errors.ExecutorError("JACOV_MAVEN_PLUGIN_POM_MISSING", "The selected JACOV Maven plugin POM cannot be extracted", [str(tool)]) from exc
    target.write_bytes(contents)
    return {"path": str(target), "sha256": pathutil.sha256_file(target), "size": len(contents)}


def _maven_plugin_coordinate_is_used(arguments: list[str], plugin: dict[str, str]) -> bool:
    coordinate = f"{plugin['groupId']}:{plugin['artifactId']}:{plugin['version']}:"
    return any(coordinate in argument for argument in arguments)


def _maven_local_repository(job: dict[str, Any], arguments: list[str]) -> Path:
    """Use Maven's normal user local repository unless the customer chose one explicitly."""
    prefix = "-Dmaven.repo.local="
    for index, argument in enumerate(arguments):
        if argument.startswith(prefix) and argument[len(prefix):]:
            return pathutil.canonical(argument[len(prefix):])
        if argument == "-Dmaven.repo.local" and index + 1 < len(arguments):
            return pathutil.canonical(arguments[index + 1])
    return pathutil.canonical(Path.home() / ".m2" / "repository")


def _maven_repository_argument(arguments: list[str], repository: Path) -> list[str]:
    """Preserve the normal Maven repository; replace only an explicitly selected repository."""
    if pathutil.same_path(str(repository), str(Path.home() / ".m2" / "repository")) and not any(
        argument.startswith("-Dmaven.repo.local=") or argument == "-Dmaven.repo.local"
        for argument in arguments
    ):
        return list(arguments)
    result: list[str] = []
    skip_next = False
    for index, argument in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        if argument.startswith("-Dmaven.repo.local="):
            continue
        if argument == "-Dmaven.repo.local" and index + 1 < len(arguments):
            skip_next = True
            continue
        result.append(argument)
    return [f"-Dmaven.repo.local={repository}", *result]


def _maven_repository_prefix(repository: Path) -> list[str]:
    """install_maven_plugin.cmd uses Maven's default repository unless one was selected."""
    if pathutil.same_path(str(repository), str(Path.home() / ".m2" / "repository")):
        return []
    return [f"-Dmaven.repo.local={repository}"]


def _maven_install_context(arguments: list[str]) -> list[str]:
    """Keep the Maven settings and offline policy that governed the original build."""
    result: list[str] = []
    value_options = {"-s", "--settings", "-gs", "--global-settings"}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in value_options and index + 1 < len(arguments):
            result.extend([argument, arguments[index + 1]])
            index += 2
            continue
        if argument.startswith(("-s", "-gs", "--settings=", "--global-settings=")) or argument in {"-o", "--offline", "-nsu", "--no-snapshot-updates"}:
            result.append(argument)
        index += 1
    return result


def _maven_plugin_install_spec(
    executable: Path,
    entry: Path,
    repository: Path,
    tool: Path,
    plugin: dict[str, str],
    context: list[str],
) -> dict[str, Any]:
    """Match install_maven_plugin.cmd: install the fat JAR with a generated minimal POM."""
    return _command_spec(
        executable,
        [
            *_maven_repository_prefix(repository),
            *context,
            "install:install-file",
            f"-Dfile={tool}",
            f"-DgroupId={plugin['groupId']}",
            f"-DartifactId={plugin['artifactId']}",
            f"-Dversion={plugin['version']}",
            "-Dpackaging=maven-plugin",
            "-DgeneratePom=true",
        ],
        entry.parent,
        coverage=True,
    )


def _require_javalib_lower(install_root: Path) -> Path:
    runtime = pathutil.canonical(install_root / "bin" / "tm-javalib-lower.jar")
    if not runtime.is_file():
        raise errors.ExecutorError(
            "JACOV_RUNTIME_LIBRARY_MISSING",
            "The LOWER Java runtime requires the installed tm-javalib-lower.jar",
            [str(runtime)],
        )
    return runtime


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


def _maven_javalib_argument(arguments: list[str], enabled: bool) -> list[str]:
    property_name = "-Dmaven.jacov.addDependencyJavaLib"
    filtered = [
        argument for argument in arguments
        if argument != property_name and not argument.startswith(f"{property_name}=")
    ]
    return [f"{property_name}={str(enabled).lower()}", *filtered]


def _maven_javalib_metadata(runtime: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(runtime) as archive:
            names = [
                name for name in archive.namelist()
                if name.endswith("/tm-javalib-lower/pom.properties")
            ]
            if len(names) != 1:
                raise KeyError("tm-javalib-lower pom.properties")
            properties = {}
            for line in archive.read(names[0]).decode("utf-8-sig").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    properties[key.strip()] = value.strip()
    except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise errors.ExecutorError(
            "JACOV_RUNTIME_LIBRARY_METADATA_INVALID",
            "tm-javalib-lower.jar has no usable Maven metadata",
            [str(runtime)],
        ) from exc
    if properties.get("artifactId") != "tm-javalib-lower" or not properties.get("version"):
        raise errors.ExecutorError(
            "JACOV_RUNTIME_LIBRARY_METADATA_INVALID",
            "tm-javalib-lower.jar Maven metadata is incomplete",
            [str(runtime)],
        )
    groups = []
    # The installed JAR exposes the current coordinate while the 4sp8 Maven plugin
    # resolves the runtime through this legacy group ID. Register both locally.
    for group in (properties.get("groupId"), MAVEN_JAVALIB_LEGACY_GROUP_ID):
        if group and group not in groups:
            groups.append(group)
    return {
        "artifactId": properties["artifactId"],
        "version": properties["version"],
        "groupIds": groups,
    }


def _maven_javalib_install_specs(
    executable: Path,
    entry: Path,
    repository: Path,
    runtime: Path,
    metadata: dict[str, Any],
    context: list[str],
) -> list[dict[str, Any]]:
    return [
        _command_spec(
            executable,
            [
                *_maven_repository_prefix(repository),
                *context,
                "install:install-file",
                f"-Dfile={runtime}",
                f"-DgroupId={group_id}",
                f"-DartifactId={metadata['artifactId']}",
                f"-Dversion={metadata['version']}",
                "-Dpackaging=jar",
                "-DgeneratePom=true",
            ],
            entry.parent,
            coverage=True,
        )
        for group_id in metadata["groupIds"]
    ]


def _gradle_init_script(arguments: list[str]) -> Path:
    candidates: list[str] = []
    for index, argument in enumerate(arguments):
        if argument in {"--init-script", "-I"} and index + 1 < len(arguments):
            candidates.append(arguments[index + 1])
        elif argument.startswith("--init-script="):
            candidates.append(argument.split("=", 1)[1])
    if len(candidates) != 1:
        raise errors.ExecutorError(
            "JACOV_GRADLE_INIT_SCRIPT_UNRESOLVED",
            "Gradle coverage arguments must select exactly one job-local init script",
            arguments,
        )
    selected = Path(candidates[0])
    if not selected.is_absolute():
        raise errors.ExecutorError(
            "JACOV_GRADLE_INIT_SCRIPT_NOT_ABSOLUTE",
            "The job-local Gradle init script path must be absolute",
            [str(selected)],
        )
    return pathutil.canonical(selected)


def _set_gradle_system_property(contents: str, name: str, value: str) -> str:
    expression = re.compile(
        rf"(?m)^[ \t]*System\.setProperty\(\s*['\"]{re.escape(name)}['\"]\s*,\s*['\"][^'\"]*['\"]\s*\)[ \t]*$"
    )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    replacement = f'System.setProperty("{name}", "{escaped}")'
    updated, count = expression.subn(replacement, contents)
    if count > 1:
        raise errors.ExecutorError(
            "JACOV_GRADLE_PROPERTY_AMBIGUOUS",
            f"The job-local Gradle init script defines {name} more than once",
        )
    return updated if count == 1 else f"{replacement}\n{contents}"


def _maven_install_executable(install_root: Path, build_executable: Path) -> Path:
    candidates = (
        install_root / "utils" / "apache-maven" / "bin" / "mvn.cmd",
        install_root / "utils" / "apache-maven" / "bin" / "mvn",
    )
    return next((pathutil.canonical(path) for path in candidates if path.is_file()), build_executable)


def _prepare_gradle_runtime(arguments: list[str], job: dict[str, Any]) -> dict[str, Any]:
    init_script = _gradle_init_script(arguments)
    temporary = pathutil.canonical(job["directories"]["temporary"])
    if not init_script.is_file() or not pathutil.path_within(init_script, temporary):
        raise errors.ExecutorError(
            "JACOV_GRADLE_INIT_SCRIPT_NOT_JOB_LOCAL",
            "Only the job-local Gradle init script may be changed for JACOV coverage",
            [str(init_script), str(temporary)],
        )
    try:
        contents = init_script.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.ExecutorError(
            "JACOV_GRADLE_INIT_SCRIPT_INVALID",
            "The job-local Gradle init script cannot be read",
            [str(init_script)],
        ) from exc
    contents = _set_gradle_system_property(contents, "jacov.addDependencyJavaLib", "false")
    contents = _set_gradle_system_property(contents, "jacov.javaLibJarDir", "")
    init_script.write_text(contents, encoding="utf-8")
    return {
        "mode": "GRADLE_COVERAGE_INSTRUMENTATION",
        "runtimeSelection": "DEFERRED_TO_EXECUTION",
        "initScript": str(init_script),
        "addDependencyJavaLib": False,
        "javaLibJarDir": "",
    }


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


def _java_instrumentation_proof(log: Path) -> dict[str, Any]:
    result_pattern = re.compile(
        r"## JAVA SOURCE COVERAGE BUILD RESULTS ##.*?"
        r"\[\s*SUCCESS\s*\]\s*:\s*(\d+).*?"
        r"\[\s*FAILURE\s*\]\s*:\s*(\d+).*?"
        r"\[\s*IGNORE\s*\]\s*:\s*(\d+)",
        re.IGNORECASE | re.DOTALL,
    )
    text = "\n".join(processes.iter_output_lines(log))
    aggregate_results = [
        {"success": int(success), "failure": int(failure), "ignore": int(ignore)}
        for success, failure, ignore in result_pattern.findall(text)
    ]
    file_result_pattern = re.compile(
        r"^\s*\[\s*(SUCCESS|FAILURE|IGNORE)\s*\]\s*:\s*(.+?\.java)(?:\s+\(Cause\s*:.*)?\s*$",
        re.IGNORECASE,
    )

    def path_key(value: str) -> str:
        return re.sub(r"/+", "/", value.strip().replace("\\", "/")).rstrip("/").casefold()

    file_results: list[dict[str, str]] = []
    active_gradle_source_root: str | None = None
    maven_jacov_block = False
    uploaded_sources: set[str] = set()
    for line in text.splitlines():
        if re.match(r"^\s*>\s*Task\s+", line, re.IGNORECASE):
            active_gradle_source_root = None
        instrumenting = re.search(r"\[COVER\]\s+Instrumenting\s*:\s*(.+?)\s+->", line, re.IGNORECASE)
        if instrumenting:
            active_gradle_source_root = path_key(instrumenting.group(1))
        maven_plugin = re.match(r"^\s*\[INFO\]\s+---\s+(.+?)\s+---\s*$", line, re.IGNORECASE)
        if maven_plugin:
            maven_jacov_block = bool(re.search(r"\bjacov:[^:\s]+:setup\b", maven_plugin.group(1), re.IGNORECASE))
        uploaded = re.search(r"uploadJavaAnalyzeInfo\s*:\s*[^,]+,\s*(.+?\.java)(?:\s|$)", line, re.IGNORECASE)
        if uploaded:
            uploaded_sources.add(path_key(uploaded.group(1)))
        file_result = file_result_pattern.match(line)
        if not file_result:
            continue
        status, source = file_result.groups()
        source_key = path_key(source)
        from_gradle_instrumentation = bool(
            active_gradle_source_root
            and (source_key == active_gradle_source_root or source_key.startswith(active_gradle_source_root + "/"))
        )
        if maven_jacov_block or source_key in uploaded_sources or from_gradle_instrumentation:
            file_results.append({"status": status.upper(), "source": source.strip()})
    file_counts = {
        status: sum(1 for item in file_results if item["status"] == status)
        for status in ("SUCCESS", "FAILURE", "IGNORE")
    }
    jacov_file_result_context = bool(file_results)
    if aggregate_results:
        result_format = "AGGREGATE"
        success = sum(item["success"] for item in aggregate_results)
        failure = max(sum(item["failure"] for item in aggregate_results), file_counts["FAILURE"])
        ignored = sum(item["ignore"] for item in aggregate_results)
    else:
        result_format = "FILE_RESULTS" if file_results else "NONE"
        success = file_counts["SUCCESS"]
        failure = file_counts["FAILURE"]
        ignored = file_counts["IGNORE"]
    skip_marker = bool(re.search(r"(?:Skipping jacov instrumentation|Instrumentation skipped|No valid source directories)", text, re.IGNORECASE))
    error_marker = bool(re.search(r"(?:\[COVER(?:\(Jacov\))?\].*(?:error|invalid)|COVER\(Jacov\).*failed)", text, re.IGNORECASE))
    if success <= 0 or failure or skip_marker or error_marker or (not aggregate_results and not jacov_file_result_context):
        raise errors.ExecutorError(
            "JACOV_PROCESSING_NOT_PROVED",
            "The coverage build did not report a successful, non-skipped JACOV source-processing result",
            [str(log)],
        )
    return {
        "kind": "JAVA_JACOV",
        "resultFormat": result_format,
        "resultGroups": aggregate_results,
        "fileResults": file_results,
        "jacovFileResultContext": jacov_file_result_context,
        "successCount": success,
        "failureCount": failure,
        "ignoreCount": ignored,
        "skipped": skip_marker,
        "errorObserved": error_marker,
        "evidence": [str(log)],
    }

def run_java(
    job: dict[str, Any], evidence: dict[str, Any], choices: dict[str, Any], stages: list[dict[str, Any]],
    args: Any, record: Callable[..., dict[str, Any]], ensure_authentication: Callable[[], None],
    finalize_survey: Callable[..., dict[str, Any]],
    write_original_evidence: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
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
        systems = list(data.get("confirmedBuildSystems") or data.get("buildSystemCandidates") or [])
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
        observer = _gradle_observer(job)
        spec = {**spec, "arguments": [*spec["arguments"], "--init-script", str(observer)]}
        plan = {**plan, "commands": {**plan["commands"], "build": spec}}
    record(job, stages, 4, "resolve-original-build-command", envelopes.envelope("RESOLVING_ORIGINAL_BUILD_COMMAND", "SUCCEEDED", {"plan": plan}))
    finalize_survey(job, stages, plan)
    if not args.execute:
        raise WorkflowWaiting("ORIGINAL_BUILD_EXECUTION_REQUIRED", "The original Java build must run", {"execute": True}, decision_owner="USER", purpose="MUTATION_PERMISSION")
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
         "cliArguments": choices.get("javaCoverageArguments") if system == "COMMAND_LINE_CANDIDATE" else None},
        lambda: envelopes.envelope("RESOLVING_JAVA_ORIGINAL_EVIDENCE", "SUCCEEDED", _postbuild_choices(job, choices, command, install, system)))["data"]
    artifacts = [Path(p) for p in observed["artifacts"]]
    major, java_home, tool = observed["major"], Path(observed["javaHome"]), Path(observed["tool"])
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
    coverage_arguments = choices.get("javaCoverageArguments")
    if coverage_arguments is None and system == "GRADLE":
        init_script = _gradle_coverage_script(job, tool)
        coverage_arguments = [*_without_observer(spec["arguments"], observer), "--init-script", str(init_script)]
    elif coverage_arguments is None and system == "MAVEN":
        plugin = _maven_plugin_record(install, tool)
        coverage_arguments = [f"{plugin['groupId']}:{plugin['artifactId']}:{plugin['version']}:setup", *spec["arguments"]]
    if not isinstance(coverage_arguments, list) or not coverage_arguments or not all(isinstance(value, str) for value in coverage_arguments):
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
    # Authentication is process-local. A saved successful stage is not a credential.
    ensure_authentication()
    network = _check_java_connection(jacov_config)
    record(job, stages, 7, "java-connection-check", envelopes.envelope("CHECKING_JAVA_CONNECTION", "SUCCEEDED", network))
    maven_install: dict[str, Any] | None = None
    maven_plugin: dict[str, str] | None = None
    runtime_dependency: dict[str, Any] | None = None
    coverage_effective_arguments = coverage_arguments
    if system == "MAVEN":
        plugin = _maven_plugin_record(install, tool)
        maven_plugin = plugin
        if not _maven_plugin_coordinate_is_used(coverage_arguments, plugin):
            raise errors.ExecutorError(
                "JACOV_MAVEN_COORDINATE_UNRESOLVED",
                "Coverage arguments do not reference the selected JACOV Maven plugin coordinate",
                [str(tool), *coverage_arguments],
            )
        original_arguments = list(spec.get("arguments") or [])
        repository = _maven_local_repository(job, [*original_arguments, *coverage_arguments])
        install_context = _maven_install_context(original_arguments)
        install_executable = _maven_install_executable(install_root, executable)
        install_spec = _maven_plugin_install_spec(install_executable, entry, repository, tool, plugin, install_context)
        plugin_destination = repository / plugin["groupId"].replace(".", "/") / plugin["artifactId"] / plugin["version"] / f"{plugin['artifactId']}-{plugin['version']}.jar"
        install_command = _maven_install_once(job, stages, record, "install-jacov-maven-artifact", install_spec, tool, plugin_destination)
        runtime = _require_javalib_lower(install_root)
        runtime_metadata = _maven_javalib_metadata(runtime)
        runtime_commands = [
            _maven_install_once(
                job, stages, record, f"install-tm-javalib-lower-{index}", runtime_spec, runtime,
                repository / runtime_metadata["groupIds"][index - 1].replace(".", "/") / runtime_metadata["artifactId"] / runtime_metadata["version"] / f"{runtime_metadata['artifactId']}-{runtime_metadata['version']}.jar",
            )
            for index, runtime_spec in enumerate(
                _maven_javalib_install_specs(install_executable, entry, repository, runtime, runtime_metadata, install_context),
                start=1,
            )
        ]
        runtime_dependency = {
            "mode": "MAVEN_COVERAGE_INSTRUMENTATION",
            "runtimeSelection": "DEFERRED_TO_EXECUTION",
            "addDependencyJavaLib": False,
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
        coverage_effective_arguments = _maven_repository_argument(
            _maven_javalib_argument(coverage_arguments, False), repository
        )
    elif system == "GRADLE":
        supplied_init = _gradle_init_script(coverage_arguments)
        if supplied_init.is_file() and pathutil.path_within(supplied_init, job["directories"]["temporary"]):
            supplied_init.write_text(_replace_gradle_jar(supplied_init.read_text(encoding="utf-8-sig"), tool), encoding="utf-8")
        runtime_dependency = _prepare_gradle_runtime(coverage_arguments, job)
        record(
            job,
            stages,
            12,
            "prepare-jacov-gradle-runtime",
            envelopes.envelope("PREPARING_JACOV_GRADLE_RUNTIME", "SUCCEEDED", runtime_dependency),
        )
    coverage_environment = {**spec.get("environmentDelta", {}), **_jacov_environment(jacov_config)}
    coverage_spec = {**spec, "arguments": coverage_effective_arguments,
                     "environmentDelta": coverage_environment, "coverToolEnvironment": True}
    coverage_inputs = {"command": coverage_spec, "artifacts": [str(p) for p in artifacts],
                       "sourceFingerprint": project_index.input_fingerprint(fileio.load_json(Path(job["directories"]["temporary"]) / "project-index.json")),
                       "jar": pathutil.sha256_file(tool), "jdk": str(java_home),
                       "configDigest": pathutil.sha256_file(Path(jacov_config["path"])),
                       "initHash": pathutil.sha256_file(_gradle_init_script(coverage_arguments)) if system == "GRADLE" else None}
    verified = state_store.run_checkpointed(job, stages, record, 13, "java-coverage-build", coverage_inputs,
        lambda: _verified_coverage(job, stages, record, artifacts, coverage_spec, spec), validate=_coverage_outputs_unchanged)["data"]
    coverage_artifacts = [Path(p) for p in verified["artifacts"]]
    build_result = {
        **verified,
        "jacovTool": str(tool),
        "mavenPluginInstall": maven_install,
        "runtimeDependency": runtime_dependency,
    }
    execution_guidance = _java_execution_guidance(install_root, runtime_dependency)
    build_result["executionGuidance"] = execution_guidance
    persistent_assets = (
        [runtime_dependency["initScript"], str(tool)]
        if system == "GRADLE" and isinstance(runtime_dependency, dict)
        else []
    )
    return {
        **build_result,
        "coverageVerified": True,
        "_manualCoverage": {
            "buildSystem": f"JAVA_{system}",
            "commands": [coverage_spec],
            "assets": persistent_assets,
            "artifacts": [str(path) for path in coverage_artifacts],
            "mode": "MANUAL_JACOV_COMMAND",
            "details": {
                "entry": str(entry),
                "projectRoot": job["projectRoot"],
                "javaHome": str(java_home),
                "javaMajorVersion": major,
                "jacovTool": str(tool),
                "runtimeSelection": "DEFERRED_TO_EXECUTION",
                "executionGuidance": execution_guidance,
                "repository": str(maven_install["repository"]) if maven_install else None,
                "mavenPlugin": maven_plugin,
            },
        },
    }


run = run_java
