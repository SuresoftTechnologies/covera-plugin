from __future__ import annotations

"""Collect deterministic C# and Java project evidence without deciding build intent."""


import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import project_index as shared_index


SCHEMA_VERSION = "cover-managed-project-detection/v3"
SKIP_DIRS = {
    ".cover-build",
    ".git",
    ".gradle",
    ".idea",
    ".vs",
    "bin",
    "build",
    "node_modules",
    "obj",
    "out",
    "target",
}
CI_DIR_NAMES = {".github", ".gitlab", ".jenkins", "ci"}
SCRIPT_SUFFIXES = {".bat", ".cmd", ".ps1", ".sh"}
YAML_SUFFIXES = {".yaml", ".yml"}
MAX_LISTED_PATHS = 200
ROOT_BUILD_COMMAND_FILES = {
    "build.bat",
    "build.cmd",
    "build.ps1",
    "build.sh",
    "configure",
    "bootstrap",
    "jenkinsfile",
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
}
JAVA_BUILD_COMMANDS = {
    "MAVEN": re.compile(r"(?i)(?<![A-Za-z0-9_.-])(?:\./|\.\\)?(?:mvnw(?:\.cmd)?|mvn(?:\.cmd)?)(?![A-Za-z0-9_.-])"),
    "GRADLE": re.compile(r"(?i)(?<![A-Za-z0-9_.-])(?:\./|\.\\)?(?:gradlew(?:\.bat)?|gradle(?:\.bat)?)(?![A-Za-z0-9_.-])"),
}
CSHARP_BUILD_COMMANDS = {
    "DOTNET": re.compile(r"(?i)(?<![A-Za-z0-9_.-])(?:dotnet(?:\.exe)?)\s+build\b"),
    "MSBUILD": re.compile(r"(?i)(?<![A-Za-z0-9_.-])(?:msbuild(?:\.exe)?)(?![A-Za-z0-9_.-])"),
}
TARGET_FRAMEWORK_PROPERTIES = {"TargetFramework", "TargetFrameworks", "TargetFrameworkVersion"}


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _sort_paths(paths: Iterable[str]) -> list[str]:
    return sorted(set(paths), key=lambda value: value.casefold())


def _limited(paths: Iterable[str]) -> tuple[list[str], bool]:
    ordered = _sort_paths(paths)
    return ordered[:MAX_LISTED_PATHS], len(ordered) > MAX_LISTED_PATHS


def _is_runtime_candidate(relative_path: Path) -> bool:
    name = relative_path.name.casefold()
    parts = {part.casefold() for part in relative_path.parts}
    if name.startswith("dockerfile") or name in {
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
        "chart.yaml",
        "jenkinsfile",
        ".gitlab-ci.yml",
        ".gitlab-ci.yaml",
    }:
        return True
    if relative_path.suffix.casefold() in SCRIPT_SUFFIXES:
        return True
    if parts & CI_DIR_NAMES:
        return True
    if relative_path.suffix.casefold() in YAML_SUFFIXES and parts & {
        "deploy",
        "deployment",
        "helm",
        "k8s",
        "kubernetes",
        "manifests",
    }:
        return True
    return False


def _is_build_command_evidence_file(relative_path: Path) -> bool:
    parts = tuple(part.casefold() for part in relative_path.parts)
    name = relative_path.name.casefold()
    if len(parts) == 1 and name in ROOT_BUILD_COMMAND_FILES:
        return True
    if len(parts) == 3 and parts[:2] == (".github", "workflows") and relative_path.suffix.casefold() in YAML_SUFFIXES:
        return True
    if parts == (".teamcity", "settings.kts"):
        return True
    return False


def _java_build_command_evidence(path: Path, relative_path: Path) -> list[dict[str, object]]:
    if not _is_build_command_evidence_file(relative_path):
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[: 512 * 1024]
    except OSError:
        return []
    evidence: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "::")) or stripped.casefold().startswith("rem "):
            continue
        for build_system, pattern in JAVA_BUILD_COMMANDS.items():
            match = pattern.search(line)
            if match and _looks_like_command_invocation(line[: match.start()]):
                evidence.append(
                    {
                        "buildSystem": build_system,
                        "path": relative_path.as_posix(),
                        "line": line_number,
                        "executableToken": match.group(0),
                    }
                )
    return evidence


def _first_option(line: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            value = next((group for group in match.groups() if group is not None), None)
            if value is not None:
                return value.strip().strip('"\'')
    return None


def _csharp_build_command_evidence(path: Path, relative_path: Path) -> list[dict[str, object]]:
    if not _is_build_command_evidence_file(relative_path):
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[: 512 * 1024]
    except OSError:
        return []
    evidence: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "::")) or stripped.casefold().startswith("rem "):
            continue
        for build_system, pattern in CSHARP_BUILD_COMMANDS.items():
            match = pattern.search(line)
            if not match or not _looks_like_command_invocation(line[: match.start()]):
                continue
            record: dict[str, object] = {
                "buildSystem": build_system,
                "path": relative_path.as_posix(),
                "line": line_number,
                "executableToken": match.group(1) if match.lastindex else match.group(0).split()[0],
            }
            target = _first_option(
                line,
                (
                    r"(?:^|\s)(?:\"([^\"]+\.(?:slnx?|csproj))\"|'([^']+\.(?:slnx?|csproj))'|([^\s]+\.(?:slnx?|csproj)))",
                ),
            )
            configuration = _first_option(
                line,
                (
                    r"(?:--configuration|-c)\s+(\"[^\"]+\"|'[^']+'|[^\s]+)",
                    r"(?:/p:|-p:|/property:)Configuration=(\"[^\"]+\"|'[^']+'|[^\s]+)",
                ),
            )
            platform = _first_option(
                line,
                (r"(?:/p:|-p:|/property:)Platform=(\"[^\"]+\"|'[^']+'|[^\s]+)",),
            )
            framework = _first_option(
                line,
                (
                    r"(?:--framework|-f)\s+(\"[^\"]+\"|'[^']+'|[^\s]+)",
                    r"(?:/p:|-p:|/property:)TargetFramework=(\"[^\"]+\"|'[^']+'|[^\s]+)",
                ),
            )
            for key, value in (
                ("target", target),
                ("configuration", configuration),
                ("platform", platform),
                ("targetFramework", framework),
            ):
                if value:
                    record[key] = value
            evidence.append(record)
    return evidence


def _framework_declarations(path: Path, root: Path) -> dict[str, object]:
    relative = _relative(root, path)
    try:
        project = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        return {
            "path": relative,
            "sdkStyle": False,
            "declarations": [],
            "declaredFrameworks": [],
            "selection": "MODEL_OR_ORIGINAL_BUILD_REQUIRED",
            "parseError": f"{type(exc).__name__}: {exc}",
        }

    sources: list[tuple[Path, ET.Element]] = []
    current = path.parent
    while True:
        candidate = current / "Directory.Build.props"
        if candidate.is_file():
            try:
                sources.append((candidate, ET.parse(candidate).getroot()))
            except (OSError, ET.ParseError):
                pass
            break
        if current == root or root not in current.parents:
            break
        current = current.parent
    sources.append((path, project))

    declarations: list[dict[str, object]] = []
    declared: list[str] = []
    for source_path, source_root in sources:
        for group in source_root.iter():
            if group.tag.rsplit("}", 1)[-1] != "PropertyGroup":
                continue
            group_condition = group.attrib.get("Condition")
            for node in group:
                property_name = node.tag.rsplit("}", 1)[-1]
                if property_name not in TARGET_FRAMEWORK_PROPERTIES or not node.text:
                    continue
                values = [value.strip() for value in node.text.split(";") if value.strip()]
                condition = node.attrib.get("Condition") or group_condition
                declarations.append(
                    {
                        "property": property_name,
                        "values": values,
                        "condition": condition,
                        "source": _relative(root, source_path),
                    }
                )
                declared.extend(values)

    declared_frameworks = _sort_paths(declared)
    conditional_or_dynamic = any(
        item.get("condition") or any("$(" in value for value in item["values"])
        for item in declarations
    )
    has_multitarget_declaration = any(item["property"] == "TargetFrameworks" for item in declarations)
    if not declarations or conditional_or_dynamic:
        selection = "MODEL_OR_ORIGINAL_BUILD_REQUIRED"
    elif len(declared_frameworks) == 1:
        selection = "AUTO_SINGLE_DECLARATION"
    elif has_multitarget_declaration and len(declarations) == 1:
        selection = "AUTO_ALL_DECLARED"
    else:
        selection = "MODEL_OR_ORIGINAL_BUILD_REQUIRED"
    return {
        "path": relative,
        "sdkStyle": bool(project.attrib.get("Sdk")),
        "declarations": declarations,
        "declaredFrameworks": declared_frameworks,
        "selection": selection,
        "parseError": None,
    }


def _global_json_record(project_path: Path, root: Path) -> dict[str, object] | None:
    current = project_path.parent
    while True:
        candidate = current / "global.json"
        if candidate.is_file():
            try:
                value = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                return {"path": _relative(root, candidate), "parseError": f"{type(exc).__name__}: {exc}"}
            sdk = value.get("sdk") if isinstance(value, dict) else None
            return {
                "path": _relative(root, candidate),
                "version": sdk.get("version") if isinstance(sdk, dict) else None,
                "rollForward": sdk.get("rollForward") if isinstance(sdk, dict) else None,
                "allowPrerelease": sdk.get("allowPrerelease") if isinstance(sdk, dict) else None,
                "parseError": None,
            }
        if current == root or root not in current.parents:
            return None
        current = current.parent


def _looks_like_command_invocation(raw_prefix: str) -> bool:
    prefix = raw_prefix.strip().casefold()
    if not prefix:
        return True
    if prefix in {"-", "&", "call", "run:", "script:", "command:", "commandline:"}:
        return True
    if prefix.endswith(("&&", "||", ";")):
        return True
    return bool(re.fullmatch(r"(?:-|\w+\s*=\s*)?(?:run|script|command|commandline):?\s*", prefix)) or bool(
        re.fullmatch(r"(?:sh|bat|powershell)\s*[('\"]\s*", prefix)
    )


def _managed_files(root: Path, index: dict[str, Any] | None) -> Iterable[tuple[Path, Path]]:
    if index is not None:
        if Path(index.get("projectRoot", "")).resolve(strict=False) != root:
            raise ValueError("project index does not belong to project root")
        for item in shared_index.entries(index):
            yield Path(item["path"]), Path(item["relativePath"])
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            (name for name in dirnames if name.casefold() not in SKIP_DIRS),
            key=str.casefold,
        )
        current = Path(directory)
        for filename in sorted(filenames, key=str.casefold):
            path = current / filename
            yield path, path.relative_to(root)


def detect_managed(root: Path, index: dict[str, Any] | None = None) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    csharp_sources: list[str] = []
    java_sources: list[str] = []
    solutions: list[str] = []
    csharp_projects: list[str] = []
    native_projects: list[str] = []
    native_build_entries: list[str] = []
    maven_files: list[str] = []
    gradle_files: list[str] = []
    java_build_command_evidence: list[dict[str, object]] = []
    csharp_build_command_evidence: list[dict[str, object]] = []
    runtime_candidates: list[str] = []

    for path, relative in _managed_files(root, index):
            relative_text = relative.as_posix()
            filename = path.name
            lowered_name = filename.casefold()
            suffix = path.suffix.casefold()

            if suffix == ".cs":
                csharp_sources.append(relative_text)
            elif suffix == ".java":
                java_sources.append(relative_text)
            elif suffix in {".sln", ".slnx"}:
                solutions.append(relative_text)
            elif suffix == ".csproj":
                csharp_projects.append(relative_text)
            elif suffix in {".vcproj", ".vcxproj"}:
                native_projects.append(relative_text)

            if lowered_name in {"cmakelists.txt", "makefile", "gnumakefile"}:
                native_build_entries.append(relative_text)

            if lowered_name == "pom.xml" or lowered_name in {"mvnw", "mvnw.cmd"}:
                maven_files.append(relative_text)
            if lowered_name in {
                "build.gradle",
                "build.gradle.kts",
                "settings.gradle",
                "settings.gradle.kts",
                "gradlew",
                "gradlew.bat",
            }:
                gradle_files.append(relative_text)
            if _is_runtime_candidate(relative):
                runtime_candidates.append(relative_text)
            java_build_command_evidence.extend(_java_build_command_evidence(path, relative))
            csharp_build_command_evidence.extend(_csharp_build_command_evidence(path, relative))

    csharp_source_list, csharp_truncated = _limited(csharp_sources)
    java_source_list, java_truncated = _limited(java_sources)
    runtime_list, runtime_truncated = _limited(runtime_candidates)

    csharp_detected = bool(csharp_sources or csharp_projects)
    java_detected = bool(java_sources or maven_files or gradle_files)
    native_detected = bool(native_projects or native_build_entries)

    detected_routes = [
        name for name, detected in (
            ("CSHARP", csharp_detected),
            ("JAVA", java_detected),
            ("NATIVE", native_detected),
        ) if detected
    ]
    if len(detected_routes) > 1:
        routing = "WAITING_MODEL_MIXED_BUILD_GRAPH"
    elif csharp_detected:
        routing = "CSHARP"
    elif java_detected:
        routing = "JAVA"
    else:
        routing = "NOT_MANAGED"

    java_build_system_candidates: list[str] = []
    if maven_files:
        java_build_system_candidates.append("MAVEN")
    if gradle_files:
        java_build_system_candidates.append("GRADLE")
    if java_sources and not java_build_system_candidates:
        java_build_system_candidates.append("COMMAND_LINE_CANDIDATE")
    confirmed_build_systems = _sort_paths(
        item["buildSystem"] for item in java_build_command_evidence if item["buildSystem"] in java_build_system_candidates
    )
    if len(confirmed_build_systems) == 1:
        build_system_decision = "CONFIRMED_BY_PROJECT_COMMAND"
    else:
        build_system_decision = "MODEL_OR_USER_REQUIRED"

    csharp_frameworks = [_framework_declarations(root / path, root) for path in _sort_paths(csharp_projects)]
    csharp_global_json = [
        record
        for project in _sort_paths(csharp_projects)
        if (record := _global_json_record(root / project, root)) is not None
    ]
    csharp_global_json = list({record["path"]: record for record in csharp_global_json}.values())
    csharp_build_system_candidates: list[str] = []
    if csharp_projects:
        csharp_build_system_candidates.append("MSBUILD")
    if any(record["sdkStyle"] for record in csharp_frameworks) or csharp_global_json:
        csharp_build_system_candidates.append("DOTNET")
    confirmed_csharp_build_systems = _sort_paths(
        item["buildSystem"]
        for item in csharp_build_command_evidence
        if item["buildSystem"] in csharp_build_system_candidates
    )
    if len(confirmed_csharp_build_systems) == 1:
        csharp_build_system_decision = "CONFIRMED_BY_PROJECT_COMMAND"
    else:
        csharp_build_system_decision = "MODEL_OR_USER_REQUIRED"
    command_frameworks = _sort_paths(
        str(item["targetFramework"])
        for item in csharp_build_command_evidence
        if item.get("targetFramework")
    )
    if len(command_frameworks) > 1:
        target_framework_decision = "MODEL_OR_USER_REQUIRED"
        selected_target_frameworks = []
    elif len(command_frameworks) == 1:
        target_framework_decision = "CONFIRMED_BY_PROJECT_COMMAND"
        selected_target_frameworks = command_frameworks
    elif len(csharp_frameworks) == 1 and csharp_frameworks[0]["selection"] in {
        "AUTO_SINGLE_DECLARATION",
        "AUTO_ALL_DECLARED",
    }:
        target_framework_decision = "AUTO_SELECTED_DECLARED_SCOPE"
        selected_target_frameworks = list(csharp_frameworks[0]["declaredFrameworks"])
    else:
        target_framework_decision = "MODEL_OR_USER_REQUIRED"
        selected_target_frameworks = []

    return {
        "schemaVersion": SCHEMA_VERSION,
        "projectRoot": str(root),
        "routing": routing,
        "csharp": {
            "detected": csharp_detected,
            "sourceCount": len(csharp_sources),
            "sources": csharp_source_list,
            "sourcesTruncated": csharp_truncated,
            "solutions": _sort_paths(solutions),
            "projectFiles": _sort_paths(csharp_projects),
            "nativeProjectFiles": _sort_paths(native_projects),
            "nativeBuildEntries": _sort_paths(native_build_entries),
            "buildSystemCandidates": csharp_build_system_candidates,
            "confirmedBuildSystems": confirmed_csharp_build_systems,
            "buildSystemDecision": csharp_build_system_decision,
            "commandEvidence": csharp_build_command_evidence,
            "projectFrameworks": csharp_frameworks,
            "targetFrameworkDecision": target_framework_decision,
            "selectedTargetFrameworks": selected_target_frameworks,
            "globalJson": csharp_global_json,
        },
        "java": {
            "detected": java_detected,
            "sourceCount": len(java_sources),
            "sources": java_source_list,
            "sourcesTruncated": java_truncated,
            "buildSystemCandidates": java_build_system_candidates,
            "confirmedBuildSystems": confirmed_build_systems,
            "buildSystemDecision": build_system_decision,
            "commandEvidence": java_build_command_evidence,
            "mavenFiles": _sort_paths(maven_files),
            "gradleFiles": _sort_paths(gradle_files),
        },
        "runtimeEvidence": {
            "candidateFiles": runtime_list,
            "candidateFilesTruncated": runtime_truncated,
            "decision": "MODEL_OR_USER_REQUIRED",
            "note": "Deployment files are evidence only; they do not prove MSA or JavaLib Agent use.",
        },
    }


def managed_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect C# and Java build/deployment candidates without guessing customer intent."
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        result = detect_managed(args.project_root)
    except (FileNotFoundError, NotADirectoryError) as error:
        parser.error(f"invalid project root: {error}")

    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


"""Classify STM32CubeIDE managed-build evidence without opening the IDE."""


STM32_SCHEMA = "cover-stm32cubeide-discovery/v1"
STM32_MARKERS = ("com.st.stm32cube", "fr.ac6.managedbuild", "stm32cubeide", "arm-none-eabi-")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _project_name(project_file: Path) -> str | None:
    try:
        root = ET.parse(project_file).getroot()
    except (OSError, ET.ParseError):
        return None
    for node in root.iter():
        if _xml_local_name(node.tag) == "name" and node.text and node.text.strip():
            return node.text.strip()
    return None


def _cproject_details(cproject: Path) -> dict[str, Any]:
    text = cproject.read_text(encoding="utf-8-sig", errors="replace")
    lowered = text.casefold()
    markers = sorted(marker for marker in STM32_MARKERS if marker in lowered)
    configurations: list[str] = []
    tools: list[str] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {"markers": markers, "configurations": [], "toolCommands": [], "parseError": str(exc)}
    for node in root.iter():
        local = _xml_local_name(node.tag)
        if local in {"cconfiguration", "configuration"}:
            name = node.attrib.get("name")
            if name and name not in configurations:
                configurations.append(name)
        command = node.attrib.get("command")
        if command and "arm-none-eabi-" in command.casefold() and command not in tools:
            tools.append(command)
    return {
        "markers": markers,
        "configurations": sorted(configurations, key=str.casefold),
        "toolCommands": sorted(tools, key=str.casefold),
    }


def detect_stm32cubeide(
    project_root: str | Path,
    index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project_root must be a directory")
    projects: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    if index is not None:
        if Path(index.get("projectRoot", "")).resolve(strict=False) != root:
            raise ValueError("project index does not belong to project root")
        project_files = [
            Path(item["path"])
            for item in shared_index.entries(index)
            if item.get("lowerName") == ".project"
        ]
        indexed_ioc = [
            Path(item["path"])
            for item in shared_index.entries(index)
            if item.get("suffix") == ".ioc"
        ]
    else:
        project_files = [
            path for path in root.rglob(".project")
            if not any(part.casefold() in SKIP_DIRS for part in path.relative_to(root).parts)
        ]
        indexed_ioc = []
    for project_file in project_files:
        directory = project_file.parent
        cproject = directory / ".cproject"
        ioc_files = sorted(
            (path for path in indexed_ioc if path.parent == directory) if index is not None else directory.glob("*.ioc"),
            key=lambda value: value.name.casefold(),
        )
        if not cproject.is_file():
            candidates.append({"type": "ECLIPSE_PROJECT_WITHOUT_CPROJECT", "path": str(project_file.resolve())})
            continue
        details = _cproject_details(cproject)
        confirmed = bool(details["markers"] or ioc_files)
        record = {
            "projectName": _project_name(project_file) or directory.name,
            "projectDirectory": str(directory.resolve()),
            "projectFile": str(project_file.resolve()),
            "cprojectFile": str(cproject.resolve()),
            "iocFiles": [str(path.resolve()) for path in ioc_files],
            **details,
        }
        if confirmed:
            projects.append(record)
        else:
            candidates.append({"type": "GENERIC_ECLIPSE_CDT_PROJECT", "path": str(cproject.resolve())})
    status = "CONFIRMED" if projects else "CANDIDATE" if candidates else "NOT_DETECTED"
    return {
        "schema": STM32_SCHEMA,
        "status": status,
        "confidence": "HIGH" if projects else "LOW" if candidates else "NONE",
        "routingRecommendation": "STM32CUBEIDE" if projects else "MODEL_REVIEW" if candidates else "GENERAL_BUILD_DISCOVERY",
        "projectRoot": str(root),
        "projects": sorted(projects, key=lambda value: (value["projectName"].casefold(), value["projectDirectory"].casefold())),
        "candidateEvidence": candidates,
        "expectedToolRoles": ["c_compiler", "cxx_compiler", "linker", "ar"],
    }


def stm32cubeide_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = detect_stm32cubeide(args.project_root)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"managed", "stm32cubeide"}:
        raise SystemExit("usage: project_discovery.py {managed|stm32cubeide} [arguments]")
    command = args.pop(0)
    return managed_main(args) if command == "managed" else stm32cubeide_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
