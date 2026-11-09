"""Gradle observation and JACOV init script preparation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cover_build.common import pathutil
from cover_build.core import errors
from cover_build.pipelines.java import build_runtime


GRADLE_OBSERVATION = '''
allprojects {
    tasks.withType(org.gradle.api.tasks.compile.JavaCompile).configureEach {
        doFirst {
            def compiler = javaCompiler.getOrNull()
            if (compiler != null) {
                println("COVER_OBS_JAVA_HOME=" + compiler.metadata.installationPath.asFile.absolutePath)
                println("COVER_OBS_JAVA_VERSION=" + compiler.metadata.languageVersion.asInt())
            }
        }
    }
    tasks.withType(org.gradle.api.tasks.bundling.AbstractArchiveTask).configureEach {
        doLast { println("COVER_OBS_ARTIFACT=" + archiveFile.get().asFile.absolutePath) }
    }
}
'''


def _gradle_observer(job: dict[str, Any]) -> Path:
    path = Path(job["directories"]["temporary"]) / "observe-gradle.init"
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
    updated, count = expression.subn(lambda _: replacement, contents)
    if count > 1:
        raise errors.ExecutorError(
            "JACOV_GRADLE_PROPERTY_AMBIGUOUS",
            f"The job-local Gradle init script defines {name} more than once",
        )
    return updated if count == 1 else f"{replacement}\n{contents}"


def _prepare_gradle_runtime(arguments: list[str], job: dict[str, Any],
                            runtime_plan: dict | None = None, runtime_info: dict | None = None) -> dict[str, Any]:
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
    enabled = build_runtime.uses_plugin_dependency(runtime_plan or {})
    runtime_directory = str(Path(runtime_info["path"]).parent) if enabled and runtime_info else ""
    contents = _set_gradle_system_property(contents, "jacov.addDependencyJavaLib", str(enabled).lower())
    contents = _set_gradle_system_property(contents, "jacov.javaLibJarDir", runtime_directory)
    init_script.write_text(contents, encoding="utf-8")
    return {
        "mode": "GRADLE_COVERAGE_INSTRUMENTATION",
        "runtimeSelection": "DEFERRED_TO_EXECUTION",
        "initScript": str(init_script),
        "addDependencyJavaLib": enabled,
        "javaLibJarDir": runtime_directory,
    }
