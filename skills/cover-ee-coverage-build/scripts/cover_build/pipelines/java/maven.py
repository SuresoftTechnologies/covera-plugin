"""Maven JACOV coordinates, local installation and coverage arguments."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Callable

from cover_build.core import state_store
from cover_build.pipelines.managed_common import _command_spec, _run
from cover_build.common import envelopes
from cover_build.common import pathutil
from cover_build.core import errors


MAVEN_JAVALIB_LEGACY_GROUP_ID = "com.codescroll.tm"


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


def _maven_coverage_arguments(arguments: list[str], plugin: dict[str, str]) -> list[str]:
    """Insert instrumentation after clean, which deletes JACOV's target sources."""
    # Only standalone option operands need skipping; attached values stay intact.
    value_options = {
        "-f", "--file", "-s", "--settings", "-gs", "--global-settings",
        "-t", "--toolchains", "-gt", "--global-toolchains", "-P", "--activate-profiles",
        "-pl", "--projects", "-rf", "--resume-from", "-T", "--threads",
        "-D", "--define", "-l", "--log-file", "-emp", "--encrypt-master-password",
        "-ep", "--encrypt-password",
    }
    insertion = 0
    operand = False
    for index, value in enumerate(arguments):
        if operand:
            operand = False
            continue
        if value in value_options:
            operand = True
            continue
        if value.startswith("-"):
            continue
        if value in {"clean", "post-clean", "clean:clean"} or re.fullmatch(
            r"org\.apache\.maven\.plugins:maven-clean-plugin(?::[^:]+)?:clean", value
        ):
            insertion = index + 1
    setup = f"{plugin['groupId']}:{plugin['artifactId']}:{plugin['version']}:setup"
    return [*arguments[:insertion], setup, *arguments[insertion:]]


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


def _maven_install_executable(install_root: Path, build_executable: Path) -> Path:
    candidates = (
        install_root / "utils" / "apache-maven" / "bin" / "mvn.cmd",
        install_root / "utils" / "apache-maven" / "bin" / "mvn",
    )
    return next((pathutil.canonical(path) for path in candidates if path.is_file()), build_executable)
