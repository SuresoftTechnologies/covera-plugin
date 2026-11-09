"""Select and attach one product runtime to reviewed instrumented-code JVMs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
import zipfile

from cover_build.common import pathutil
from cover_build.core.errors import ExecutorError, WorkflowWaiting


def waiting(message: str) -> None:
    raise WorkflowWaiting("JAVA_BUILD_RUNTIME_REQUIRED", message, {"javaBuildRuntime": {}},
                          decision_owner="MODEL", purpose="BUILD_ENVIRONMENT")


def validate_plan(plan: Any, system: str) -> dict:
    if not isinstance(plan, dict) or not isinstance(plan.get("reason"), str) or not plan["reason"].strip():
        waiting("원본 빌드의 테스트·프로그램 실행 여부와 런타임 적용 위치를 확인한다.")
    if plan.get("mode") == "NONE":
        if set(plan) != {"mode", "reason"}:
            waiting("실행하지 않는 계획에는 런타임 주입 항목을 넣지 않는다.")
        return plan
    if plan.get("mode") not in {"JAVA_AGENT", "LOWER"} or type(plan.get("javaMajorVersion")) is not int or plan["javaMajorVersion"] < 8:
        waiting("JAVA_AGENT 또는 LOWER와 실제 실행 JVM의 javaMajorVersion을 기록한다.")
    if plan.get("application") == "JAVA_COMMAND":
        if system in {"MAVEN", "GRADLE"}:
            waiting("JAVA_COMMAND는 원본 실행 파일 자체가 java인 직접 빌드에만 사용한다.")
        if not all(isinstance(plan.get(key), str) and plan[key].strip() for key in ("executionJvm", "classpathEvidence", "existingRuntime")):
            waiting("직접 실행 JVM과 classpath, 기존 런타임 근거를 기록한다.")
        if plan["mode"] == "LOWER":
            if type(plan.get("classpathArgumentIndex")) is not int or plan["classpathArgumentIndex"] < 1:
                waiting("LOWER를 추가할 기존 -cp/-classpath/--class-path 값의 인덱스를 확인한다.")
            if not isinstance(plan.get("configurationEvidence"), str) or not plan["configurationEvidence"].strip():
                waiting("LOWER 출력·연결 설정의 근거가 필요하다.")
        return plan
    if plan.get("application") in {"PLUGIN_DEPENDENCY", "EXISTING"}:
        for key in ("executionJvm", "classpathEvidence", "existingRuntime", "configurationEvidence"):
            if not isinstance(plan.get(key), str) or not plan[key].strip():
                waiting("선택한 JVM·클래스로더·기존 런타임·제품 설정 근거를 기록한다.")
        if plan["application"] == "PLUGIN_DEPENDENCY":
            if plan["mode"] != "LOWER" or system not in {"MAVEN", "GRADLE"}:
                waiting("PLUGIN_DEPENDENCY는 Maven/Gradle의 LOWER 의존성 추가에만 사용한다.")
            if not isinstance(plan.get("pluginDependencyEvidence"), str) or not plan["pluginDependencyEvidence"].strip():
                waiting("JACOV goal/task의 의존성 추가가 해당 실행 JVM까지 전달되는 근거가 필요하다.")
        return plan
    if plan.get("application") == "BUILD_FILES":
        if not isinstance(plan.get("files"), list) or not plan["files"]:
            waiting("대상 JVM에 런타임을 연결하는 빌드 파일의 정확한 변경을 files에 기록한다.")
        for key in ("executionJvm", "classpathEvidence", "existingRuntime"):
            if not isinstance(plan.get(key), str) or not plan[key].strip():
                waiting("실행 JVM, 클래스 로딩 경로 근거, 기존 런타임 유무를 기록한다.")
        if plan["mode"] == "LOWER" and (not isinstance(plan.get("configurationEvidence"), str) or not plan["configurationEvidence"].strip()):
            waiting("LOWER의 내장 설정과 대상 JVM 시스템 속성, 출력·연결 설정의 근거를 configurationEvidence에 기록한다.")
        return plan
    if plan.get("application") not in {None, "EXISTING_OPTIONS"}:
        waiting("application은 PLUGIN_DEPENDENCY, EXISTING, BUILD_FILES, JAVA_COMMAND 또는 EXISTING_OPTIONS여야 한다.")
    if plan["mode"] == "LOWER":
        waiting("LOWER는 BUILD_FILES로 해당 실행의 classpath/dependency 연결을 명시한다. JVM 옵션 변수에 JAR만 추가하지 않는다.")
    if system == "MAVEN":
        config = plan.get("maven")
        if (not isinstance(config, dict) or config.get("forked") is not True
                or not re.fullmatch(r"[A-Za-z][\w.-]*", str(config.get("property", "")))
                or not isinstance(config.get("value"), str)):
            waiting("Surefire/Failsafe의 별도 JVM에서 사용하는 속성명과 기존 값을 확인한다.")
        if config["property"] in {"maven.test.skip", "skipTests", "skipITs", "forkCount"}:
            waiting("테스트 생략이나 fork 설정을 런타임 주입 속성으로 사용할 수 없다.")
    elif system == "GRADLE":
        tasks = plan.get("gradleJvmTasks", plan.get("gradleTestTasks"))
        if not isinstance(tasks, list) or not tasks or not all(isinstance(t, str) and re.fullmatch(r":[\w:.-]+", t) for t in tasks):
            waiting("원본에서 실행한 Gradle Test 또는 JavaExec 태스크의 전체 경로를 지정한다.")
    else:
        name = plan.get("environmentVariable")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or name.upper() in {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "MAVEN_OPTS", "GRADLE_OPTS"}
                or not isinstance(plan.get("value"), str)):
            waiting("직접 빌드는 대상 Java 실행에만 전달되는 고객 JVM 옵션 변수를 확인한다. JVM 전역 변수는 사용하지 않는다.")
    return plan


def agent_metadata(root: Path, java_major: int) -> dict:
    agent = root / "java_agent" / "cover-javalib-agent.jar"
    if not agent.is_file():
        raise WorkflowWaiting("JAVA_BUILD_AGENT_MISSING", "제품이 생성한 실행용 JavaLib Agent를 준비해야 한다.",
                              {"javaBuildRuntime": {}}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION",
                              evidence=[str(agent)])
    try:
        with zipfile.ZipFile(agent) as jar:
            manifest = jar.read("META-INF/MANIFEST.MF").decode("utf-8").replace("\r\n ", "")
            premain = re.search(r"(?m)^Premain-Class:\s*([^\r\n]+)", manifest)
            if not premain:
                raise ValueError()
            for name in (premain[1].strip().replace(".", "/") + ".class", "com/qualityscroll/cover/runtime/ProbeStore.class"):
                header = jar.read(name)[:8]
                if len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe" or int.from_bytes(header[6:8], "big") > java_major + 44:
                    raise ValueError()
            # Read only to validate; do not publish embedded configuration values.
            config = jar.read("cover-config.properties").decode("iso-8859-1")
            server = re.search(r"(?m)^\s*Config\.SERVER_URL\s*=\s*(\S+)\s*$", config)
            origin = urlsplit(server[1].replace("\\:", ":")) if server else None
            if not origin or origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password:
                raise ValueError()
    except (OSError, ValueError, KeyError, UnicodeError, zipfile.BadZipFile):
        raise ExecutorError("JAVA_BUILD_AGENT_INVALID", "JavaLib Agent의 manifest, ProbeStore, Java 버전 또는 제품 설정을 확인해야 한다.", [str(agent)]) from None
    return {"path": str(agent.resolve()), "sha256": pathutil.sha256_file(agent), "javaMajorVersion": java_major}


def append_option(value: str, agent: str) -> str:
    if "tm-javalib-lower" in value.lower():
        raise ExecutorError("JAVA_RUNTIME_MODE_CONFLICT", "LOWER와 JavaLib Agent를 동시에 적용하지 않는다.")
    if any(c in value for c in "\r\n\x00"):
        raise ExecutorError("JAVA_BUILD_RUNTIME_INVALID", "JVM 옵션에는 줄바꿈을 넣지 않는다.")
    tokens = re.findall(r'"-javaagent:[^"]+"|-javaagent:"[^"]+"|-javaagent:\S+', value)
    covers = [t for t in tokens if "cover-javalib-agent.jar" in t.lower()]
    option = f'-javaagent:{agent.replace(chr(92), "/")}'
    if covers:
        existing = covers[0].replace('"', '').split(":", 1)[1]
        if len(covers) != 1 or not pathutil.same_path(existing, agent):
            raise ExecutorError("JAVA_RUNTIME_MODE_CONFLICT", "기존 JavaLib Agent가 선택한 파일과 다르거나 중복돼 있다.")
        return value
    return (value + " " + '"' + option + '"').strip()


def apply(plan: dict, system: str, arguments: list[str], environment: dict, agent: dict | None,
          init_script: Path | None = None) -> tuple[list[str], dict]:
    if plan["mode"] == "NONE":
        return list(arguments), dict(environment)
    assert agent is not None
    if plan.get("application") in {"BUILD_FILES", "PLUGIN_DEPENDENCY", "EXISTING", "JAVA_COMMAND"}:
        return list(arguments), dict(environment)
    args, env = list(arguments), dict(environment)
    if system == "MAVEN":
        config = plan["maven"]
        name, original = config["property"], config["value"]
        if re.search(r"[\$@]\{" + re.escape(name) + r"\}", original):
            waiting("주입할 Maven 속성이 자기 자신을 참조한다. 기존 argLine을 보존하는 별도 속성을 확인한다.")
        prefix = "-D" + name + "="
        matches = [a for a in args if a.startswith(prefix)]
        split_property = any(a == "-D" and i + 1 < len(args) and
                             args[i + 1].split("=", 1)[0] == name for i, a in enumerate(args))
        if split_property or len(matches) > 1 or (matches and matches[0][len(prefix):] != original) or "-D" + name in args:
            waiting("기존 Maven JVM 옵션과 확인한 값이 다르다. 현재 명령을 다시 확인한다.")
        args = [a for a in args if not a.startswith(prefix)]
        args.append(prefix + append_option(original, agent["path"]))
    elif system == "GRADLE":
        assert init_script is not None
        # Configure the execution JVM, not the daemon. Never replace jvmArgs.
        block = "\n// COVER_BUILD_JAVA_AGENT\n" + r'''
allprojects {
    tasks.configureEach {
        if (__TASKS__.contains(path)) {
            if (!(it instanceof org.gradle.api.tasks.testing.Test) && !(it instanceof org.gradle.api.tasks.JavaExec)) {
                throw new GradleException("JAVA_BUILD_RUNTIME_REQUIRED: selected task is not Test or JavaExec")
            }
            doFirst {
                def coverAgent = __AGENT__
                def current = (allJvmArgs ?: []).findAll { it.startsWith('-javaagent:') && it.toLowerCase().contains('cover-javalib-agent.jar') }
                if (classpath.files.any { it.name.toLowerCase().contains('tm-javalib-lower') }) {
                    throw new GradleException('JAVA_RUNTIME_MODE_CONFLICT: LOWER is already present')
                }
                if (current.size() > 1 || (current && new File(current[0].substring(11)).canonicalPath != new File(coverAgent).canonicalPath)) {
                    throw new GradleException('JAVA_RUNTIME_MODE_CONFLICT: different or duplicate JavaLib Agent')
                }
                if (!current) { jvmArgs('-javaagent:' + coverAgent) }
                println('COVER_OBS_BUILD_JAVA_AGENT=' + path)
            }
        }
    }
}
'''.replace("__TASKS__", json.dumps(plan.get("gradleJvmTasks", plan.get("gradleTestTasks")))).replace("__AGENT__", json.dumps(agent["path"].replace("\\", "/")).replace("$", "\\$"))
        content = init_script.read_text(encoding="utf-8-sig").split("\n// COVER_BUILD_JAVA_AGENT\n", 1)[0]
        init_script.write_text(content + block, encoding="utf-8")
    else:
        name, original = plan["environmentVariable"], plan["value"]
        if env.get(name, os.environ.get(name, "")) != original:
            waiting("고객 JVM 옵션 변수의 현재 값과 검토한 값이 다르다.")
        env[name] = append_option(original, agent["path"])
    return args, env


def runtime_metadata(root: Path, plan: dict) -> dict | None:
    if plan["mode"] == "NONE":
        return None
    if plan["mode"] == "JAVA_AGENT":
        return {**agent_metadata(root, plan["javaMajorVersion"]), "mode": "JAVA_AGENT"}
    jar_path = root / "bin" / "tm-javalib-lower.jar"
    if not jar_path.is_file():
        waiting("설치본 bin/tm-javalib-lower.jar를 확인한다. 다른 런타임으로 임의 대체하지 않는다.")
    try:
        with zipfile.ZipFile(jar_path) as jar:
            header = jar.read("com/qualityscroll/cover/runtime/ProbeStore.class")[:8]
            if len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe" or int.from_bytes(header[6:8], "big") > plan["javaMajorVersion"] + 44:
                raise ValueError()
            config_present = "jacoco-agent.properties" in jar.namelist()
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        raise ExecutorError("JAVA_BUILD_LOWER_INVALID", "LOWER JAR의 ProbeStore와 실행 Java 버전 호환성을 확인한다.", [str(jar_path)]) from None
    return {"path": str(jar_path.resolve()), "sha256": pathutil.sha256_file(jar_path),
            "javaMajorVersion": plan["javaMajorVersion"], "mode": "LOWER",
            "embeddedConfigurationPresent": config_present,
            "runtimeConnectionVerified": False}


def prepare_build_files(job: dict, plan: dict, runtime: dict | None,
                        integration: dict | None, tool: Path):
    """Compose source-flow and runtime edits into one backup/restore transaction."""
    if plan.get("application") != "BUILD_FILES":
        return None
    from cover_build.pipelines.java import cli
    assert runtime is not None
    combined = {}
    source_flow = isinstance(integration, dict) and integration.get("mode", "SEPARATE_OUTPUT") == "SEPARATE_OUTPUT"
    for item in [*(integration.get("files", []) if source_flow else []), *plan["files"]]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            waiting("빌드 파일 변경에는 path, sha256, replacements가 필요하다.")
        key = item["path"].replace("\\", "/")
        if key in combined:
            if combined[key].get("sha256") != item.get("sha256"):
                waiting("같은 빌드 파일의 소스 계측 연결과 런타임 연결은 같은 원본 해시를 사용한다.")
            edits = item.get("replacements")
            if not isinstance(edits, list):
                waiting("replacements는 정확한 before/after 변경 배열이어야 한다.")
            combined[key]["replacements"].extend(edits)
        else:
            combined[key] = {**item, "replacements": list(item.get("replacements") or [])}
    merged = {"mode": "SEPARATE_OUTPUT", "reason": plan["reason"], "files": list(combined.values())}
    patch, updated = cli.prepare(job, merged, tool if source_flow else Path(runtime["path"]))
    from cover_build.services import project_backup
    texts = [project_backup._decode(Path(record["path"]), data)[0]
             for record, data in zip(patch.records, updated)]
    runtime_path = runtime["path"].replace("\\", "/")
    combined_text = "\n".join(texts).replace("\\", "/")
    if runtime_path not in combined_text:
        waiting("수정된 빌드 파일은 검증한 런타임 JAR의 절대 경로를 참조해야 한다.")
    other = "tm-javalib-lower" if plan["mode"] == "JAVA_AGENT" else "cover-javalib-agent"
    if other in combined_text.lower():
        raise ExecutorError("JAVA_RUNTIME_MODE_CONFLICT", "빌드 파일에 LOWER와 JavaLib Agent가 함께 존재한다. 적용 범위를 다시 확인한다.")
    if plan["mode"] == "JAVA_AGENT" and "-javaagent:" not in combined_text:
        waiting("Agent는 대상 JVM의 시작 옵션에 -javaagent로 연결한다.")
    return patch, updated


def _plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def check_previous_failure(job: dict, plan: dict) -> None:
    marker = Path(job["directories"]["temporary"]) / "java-runtime-replan.json"
    if marker.is_file():
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("planDigest") == _plan_digest(plan):
            waiting("같은 런타임 계획에서 ProbeStore 로딩 실패가 확인됐다. 실행 JVM·클래스로더·적용 위치를 재검토하고 계획을 변경하거나 필요한 환경을 보고한다.")


def replan_after_failure(job: dict, plan: dict | None, error: ExecutorError) -> None:
    if plan is None or error.code != "COVERAGE_BUILD_FAILED":
        return
    pattern = re.compile(r"(?:ClassNotFoundException|NoClassDefFoundError).*com[./]qualityscroll[./]cover[./]runtime[./]ProbeStore")
    for item in error.evidence:
        if not isinstance(item, str) or not Path(item).is_file():
            continue
        with Path(item).open(encoding="utf-8", errors="replace") as stream:
            missing = any(pattern.search(line) for line in stream)
        if missing:
            from cover_build.common import fileio
            fileio.atomic_write_json(Path(job["directories"]["temporary"]) / "java-runtime-replan.json",
                                     {"planDigest": _plan_digest(plan), "log": item})
            raise WorkflowWaiting("JAVA_BUILD_RUNTIME_REQUIRED",
                                  "계측 클래스를 실행한 JVM에서 ProbeStore를 찾지 못했다. 복구 후 런타임 방식과 클래스 로딩 경로를 재판단한다.",
                                  {"javaBuildRuntime": {}}, decision_owner="MODEL",
                                  purpose="BUILD_ENVIRONMENT", evidence=[item])


def uses_plugin_dependency(plan: dict) -> bool:
    return plan.get("mode") == "LOWER" and plan.get("application") == "PLUGIN_DEPENDENCY"


def check_runtime_arguments(plan: dict, arguments: list[str]) -> None:
    text = " ".join(arguments).lower()
    has_agent = bool(re.search(r"-javaagent:[^\r\n]*cover-javalib-agent", text))
    has_lower = "tm-javalib-lower" in text or bool(re.search(r"(?:maven\.)?jacov\.adddependencyjavalib=true", text))
    if plan.get("application") == "EXISTING" and re.search(r"(?:maven\.)?jacov\.adddependencyjavalib=true", text):
        waiting("기존 LOWER가 JACOV 의존성 자동 추가로 연결된다면 PLUGIN_DEPENDENCY를 선택해 옵션을 보존한다.")
    if plan["mode"] == "NONE" and (has_agent or has_lower):
        waiting("실행 명령에 COVER 런타임이 이미 있다. NONE으로 제거하지 말고 실행 범위와 재사용 방식을 확인한다.")
    if has_agent and (has_lower or plan["mode"] == "LOWER"):
        raise ExecutorError("JAVA_RUNTIME_MODE_CONFLICT", "선택한 실행 명령에 LOWER와 COVER Agent가 함께 연결된다.")
    if has_lower and plan["mode"] == "JAVA_AGENT":
        raise ExecutorError("JAVA_RUNTIME_MODE_CONFLICT", "LOWER 의존성 추가와 COVER Agent를 동시에 사용할 수 없다.")


def apply_java_command(plan: dict, spec: dict, runtime: dict | None) -> dict:
    if plan.get("application") != "JAVA_COMMAND":
        return dict(spec)
    if Path(spec["executable"]).name.lower() not in {"java", "java.exe"}:
        waiting("원본 실행 파일은 java가 아니다. 스크립트 내부 Java 호출은 BUILD_FILES로 연결한다.")
    assert runtime is not None
    args = list(spec["arguments"])
    check_runtime_arguments(plan, args)
    if plan["mode"] == "JAVA_AGENT":
        if any("cover-javalib-agent" in arg.lower() for arg in args):
            waiting("기존 Agent가 있다. 실제 경로를 확인하고 EXISTING으로 재사용한다.")
        args.insert(0, "-javaagent:" + runtime["path"])
    else:
        if any("tm-javalib-lower" in arg.lower() for arg in args):
            waiting("기존 LOWER가 있다. 실제 JAR 경로와 로딩 범위를 확인하고 EXISTING으로 재사용한다.")
        index = plan["classpathArgumentIndex"]
        if "-jar" in args or index >= len(args) or args[index - 1] not in {"-cp", "-classpath", "--class-path"}:
            waiting("일반 classpath 값 위치를 확인할 수 없다. -jar/module/custom launcher는 실제 실행 설정을 확인한다.")
        if runtime["path"] not in args[index].split(os.pathsep):
            args[index] += os.pathsep + runtime["path"]
    return {**spec, "arguments": args}
