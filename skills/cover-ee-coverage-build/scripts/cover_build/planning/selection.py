"""Resolve build-system, entry, configuration, and platform choices."""

from __future__ import annotations

import platform
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


from cover_build.core.errors import WorkflowWaiting
from cover_build.core import errors


def _maven_reactor_root(candidates: list[str], project_root: Path | None) -> str | None:
    """Return the one POM whose module graph owns every discovered Maven POM."""
    if project_root is None or len(candidates) < 2:
        return candidates[0] if len(candidates) == 1 else None
    by_path = {
        (project_root / value).resolve(strict=False): value
        for value in candidates
    }
    modules: dict[Path, set[Path]] = {}
    for pom in by_path:
        try:
            root = ET.parse(pom).getroot()
        except (OSError, ET.ParseError):
            return None
        container = root.find("./{*}modules")
        children: set[Path] = set()
        if container is not None:
            for node in container.findall("./{*}module"):
                value = (node.text or "").strip()
                if not value:
                    continue
                module = (pom.parent / value).resolve(strict=False)
                module_pom = module if module.name.casefold() == "pom.xml" else module / "pom.xml"
                if module_pom in by_path:
                    children.add(module_pom)
        modules[pom] = children

    def closure(start: Path) -> set[Path]:
        found: set[Path] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(modules.get(current, ()))
        return found

    roots = [pom for pom in by_path if closure(pom) == set(by_path)]
    return by_path[roots[0]] if len(roots) == 1 else None


def java_build_entries(data: dict[str, Any], system: str, project_root: str | Path | None = None) -> list[str]:
    files = data.get("mavenFiles") if system == "MAVEN" else data.get("gradleFiles") if system == "GRADLE" else []
    candidates = [v for v in files or [] if Path(v).name.casefold() in
                  {"pom.xml", "settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"}]
    if system == "GRADLE":
        settings = [v for v in candidates if Path(v).name.casefold().startswith("settings.gradle")]
        # A single root settings file owns the root build and child build files.
        if len(settings) == 1 and all(Path(settings[0]).parent == Path(v).parent or
                Path(settings[0]).parent in Path(v).parents for v in candidates):
            return settings
    elif system == "MAVEN":
        root = _maven_reactor_root(candidates, Path(project_root) if project_root else None)
        if root:
            return [root]
    return candidates


def collect_prebuild_decision_bundle(
    discovery: dict[str, Any] | None,
    stm32cubeide_discovery: dict[str, Any] | None,
    managed: dict[str, Any],
    choices: dict[str, Any],
) -> dict[str, Any]:
    """Return every independent ambiguity visible before any expensive build."""
    unresolved: dict[str, Any] = {}
    routing = managed.get("routing")
    if routing in {
        "WAITING_MODEL_MIXED_MANAGED",
        "WAITING_MODEL_MIXED_DOTNET_NATIVE",
        "WAITING_MODEL_MIXED_BUILD_GRAPH",
    }:
        candidates = []
        if managed.get("csharp", {}).get("detected"):
            candidates.append("CSHARP")
        if managed.get("java", {}).get("detected"):
            candidates.append("JAVA")
        if routing in {"WAITING_MODEL_MIXED_DOTNET_NATIVE", "WAITING_MODEL_MIXED_BUILD_GRAPH"} and (
            managed.get("csharp", {}).get("nativeProjectFiles")
            or managed.get("csharp", {}).get("nativeBuildEntries")
        ):
            candidates.append("NATIVE")
        selected_route = choices.get("languageRoute")
        if selected_route not in candidates:
            unresolved["languageRoute"] = candidates
            return unresolved
        routing = str(selected_route)

    if routing == "CSHARP":
        data = managed["csharp"]
        supplied_plan = choices.get("originalBuildPlan")
        if isinstance(supplied_plan, dict) and str(supplied_plan.get("driver") or "").startswith("CSHARP_"):
            return unresolved
        entries = list(data.get("solutions") or data.get("projectFiles") or [])
        systems = list(data.get("confirmedBuildSystems") or data.get("buildSystemCandidates") or [])
        if len(entries) > 1 and choices.get("managedBuildEntry") not in entries:
            unresolved["managedBuildEntry"] = entries
        if len(systems) > 1 and choices.get("managedBuildSystem") not in systems:
            unresolved["managedBuildSystem"] = systems
        if data.get("targetFrameworkDecision") == "MODEL_OR_USER_REQUIRED" and not choices.get("targetFrameworks"):
            frameworks = sorted(
                {
                    framework
                    for project in data.get("projectFrameworks") or []
                    for framework in project.get("declaredFrameworks") or []
                },
                key=str.casefold,
            )
            unresolved["targetFrameworks"] = frameworks
        command_configurations = sorted(
            {str(item["configuration"]) for item in data.get("commandEvidence") or [] if item.get("configuration")},
            key=str.casefold,
        )
        if not command_configurations and not choices.get("managedConfiguration"):
            unresolved["managedConfiguration"] = []
        return unresolved

    if routing == "JAVA":
        data = managed["java"]
        supplied_plan = choices.get("originalBuildPlan")
        if isinstance(supplied_plan, dict) and str(supplied_plan.get("driver") or "").startswith("JAVA_"):
            return unresolved
        systems = list(data.get("confirmedBuildSystems") or data.get("buildSystemCandidates") or [])
        if len(systems) != 1 and choices.get("managedBuildSystem") not in systems:
            unresolved["managedBuildSystem"] = systems
        selected = choices.get("managedBuildSystem") or (systems[0] if len(systems) == 1 else None)
        build_entries = java_build_entries(data, selected, managed.get("projectRoot"))
        if len(build_entries) > 1 and choices.get("managedBuildEntry") not in build_entries:
            unresolved["managedBuildEntry"] = build_entries
        # A script match is evidence to interpret, not a validated task list.
        if not choices.get("managedTasks"):
            unresolved["managedTasks"] = []
        return unresolved

    if discovery is None:
        return unresolved
    data = discovery.get("data", {})
    systems = list(data.get("candidateBuildSystems") or [])
    stm_status = (stm32cubeide_discovery or {}).get("status")
    if stm_status == "CANDIDATE":
        systems = list(dict.fromkeys(["STM32CUBEIDE", *systems]))
    elif stm_status == "CONFIRMED":
        systems = ["STM32CUBEIDE"]
    selected = choices.get("buildSystem") or (systems[0] if len(systems) == 1 else None)
    if len(systems) > 1 and selected not in systems:
        unresolved["buildSystem"] = systems
    selected_systems = [selected] if selected in systems else systems
    if selected == "VISUAL_STUDIO":
        files = data.get("files") or []
        solutions = [item["path"] for item in files if item.get("type") == "VISUAL_STUDIO_SOLUTION"]
        projects = [item["path"] for item in files if item.get("type") == "VISUAL_CPP_PROJECT"]
        entries = solutions or projects
        entry = choices.get("buildEntry") or (entries[0] if len(entries) == 1 else None)
        if len(entries) > 1 and entry not in entries:
            unresolved["buildEntry"] = entries
        if entry:
            configs = data.get("solutionConfigurations", {}).get(entry) or data.get("projectConfigurations", {}).get(entry) or []
            if len(configs) > 1 and choices.get("configurationPlatform") not in configs:
                unresolved["configurationPlatform"] = configs
    elif any(system in {"CMAKE", "MAKE"} for system in selected_systems):
        entries = list(
            dict.fromkeys(
                entry
                for system in selected_systems
                if system in {"CMAKE", "MAKE"}
                for entry in data.get("gccBuildDrivers", {}).get(system) or []
            )
        )
        supplied_entry = choices.get("gccBuildEntry")
        if (supplied_entry is not None and supplied_entry not in entries) or (
            supplied_entry is None and len(entries) > 1
        ):
            unresolved["gccBuildEntry"] = entries
    elif selected == "STM32CUBEIDE":
        projects = list((stm32cubeide_discovery or {}).get("projects") or [])
        names = [project.get("projectName") for project in projects if project.get("projectName")]
        if len(names) > 1 and choices.get("stm32ProjectName") not in names:
            unresolved["stm32ProjectName"] = names
    return unresolved

def select_one(name: str, candidates: list[str], choices: dict[str, Any]) -> str:
    supplied = choices.get(name)
    if supplied is not None:
        if supplied not in candidates:
            raise errors.ExecutorError("INVALID_WORKFLOW_DECISION", f"{name} is not one of the discovered candidates", candidates)
        return supplied
    if len(candidates) == 1:
        return candidates[0]
    raise WorkflowWaiting(
        "AMBIGUOUS_SELECTION",
        f"Select one {name}",
        {name: candidates},
        decision_owner="MODEL",
        purpose="BUILD_ENVIRONMENT",
        evidence=candidates,
    )

def select_visual_studio(discovery: dict[str, Any], choices: dict[str, Any]) -> tuple[str, list[str], str, str]:
    data = discovery["data"]
    files = data["files"]
    solutions = [item["path"] for item in files if item["type"] == "VISUAL_STUDIO_SOLUTION"]
    projects = [item["path"] for item in files if item["type"] == "VISUAL_CPP_PROJECT"]
    legacy_projects = [item["path"] for item in files if item["type"] == "VISUAL_CPP_LEGACY_PROJECT"]
    if legacy_projects and choices.get("buildEntry") is None:
        raise WorkflowWaiting(
            "LEGACY_VISUAL_STUDIO_PROJECT_REVIEW_REQUIRED",
            "Legacy .vcproj input requires a model-selected supported MSBuild entry or an upgrade path",
            {"buildEntry": [*solutions, *legacy_projects]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=legacy_projects,
        )
    explicit_entry = choices.get("buildEntry")
    if explicit_entry is not None:
        candidates = [*solutions, *projects]
        if explicit_entry not in candidates:
            raise errors.ExecutorError(
                "INVALID_WORKFLOW_DECISION",
                "buildEntry is not one of the discovered Visual Studio solutions or projects",
                candidates,
            )
        selected = explicit_entry
    else:
        selected = select_one("buildEntry", solutions or projects, choices)
    selected_projects = projects if selected.lower().endswith(".sln") else [selected]
    if selected.lower().endswith(".sln"):
        solution_items = data.get("solutionProjects", {}).get(selected, [])
        solution_families = {
            item.get("languageFamily")
            for item in solution_items
            if item.get("languageFamily") not in {None, "UNKNOWN"}
        }
        solution_cpp_projects = [
            item.get("path")
            for item in solution_items
            if item.get("languageFamily") == "C_CPP" and item.get("path") in projects
        ]
        supplied_projects = choices.get("visualStudioProjects")
        if supplied_projects is not None:
            if (
                not isinstance(supplied_projects, list)
                or not supplied_projects
                or any(path not in solution_cpp_projects for path in supplied_projects)
            ):
                raise errors.ExecutorError(
                    "INVALID_WORKFLOW_DECISION",
                    "visualStudioProjects must be a non-empty subset of the selected solution's .vcxproj projects",
                    solution_cpp_projects,
                )
            selected_projects = list(supplied_projects)
        elif len(solution_families) > 1:
            raise WorkflowWaiting(
                "MULTI_LANGUAGE_SOLUTION_REVIEW_REQUIRED",
                "The selected solution contains multiple language families; select the C/C++ projects in scope",
                {"visualStudioProjects": solution_cpp_projects},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
                evidence=solution_items,
            )
        elif solution_cpp_projects:
            selected_projects = solution_cpp_projects
    if not selected_projects:
        raise WorkflowWaiting(
            "PREFLIGHT_PROJECT_REQUIRED",
            "No C++ project is available for MSBuild wrapper preflight",
            {"buildEntry": projects},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=projects,
        )
    configs = data["solutionConfigurations"].get(selected) or data["projectConfigurations"].get(selected) or []
    if not configs:
        configs = data["projectConfigurations"].get(selected_projects[0], [])
    if not configs:
        raise WorkflowWaiting(
            "CONFIGURATION_NOT_DISCOVERED",
            "No Configuration|Platform values were discovered",
            {},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[selected, *selected_projects],
        )
    # Existing outputs describe past builds, not the configuration requested now.
    configuration_platform = select_one("configurationPlatform", configs, choices)
    if "|" not in configuration_platform:
        raise errors.ExecutorError("INVALID_CONFIGURATION", f"Expected Configuration|Platform: {configuration_platform}")
    configuration, platform = configuration_platform.split("|", 1)
    return selected, selected_projects, configuration, platform

def select_original_visual_studio_configuration(
    discovery: dict[str, Any],
    entry: str,
    projects: list[str],
    coverage_configuration: str,
    coverage_platform: str,
    choices: dict[str, Any],
) -> tuple[str, str]:
    data = discovery["data"]
    configurations = data["solutionConfigurations"].get(entry) or data["projectConfigurations"].get(entry) or []
    if not configurations and projects:
        configurations = data["projectConfigurations"].get(projects[0], [])
    if not configurations:
        raise WorkflowWaiting(
            "CONFIGURATION_NOT_DISCOVERED",
            "No original-build Configuration|Platform values were discovered",
            {},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[entry, *projects],
        )
    selected_coverage = f"{coverage_configuration}|{coverage_platform}"
    if coverage_configuration.casefold() not in {"cover", "coverage"} and selected_coverage in configurations:
        selected = choices.get("originalConfigurationPlatform", selected_coverage)
        if selected not in configurations:
            raise errors.ExecutorError(
                "INVALID_WORKFLOW_DECISION",
                "originalConfigurationPlatform is not one of the discovered candidates",
                configurations,
            )
    else:
        native = [
            value
            for value in configurations
            if "|" in value and value.split("|", 1)[0].casefold() not in {"cover", "coverage"}
        ]
        same_platform = [value for value in native if value.split("|", 1)[1].casefold() == coverage_platform.casefold()]
        candidates = same_platform or native
        selected = select_one("originalConfigurationPlatform", candidates, choices)
    if "|" not in selected:
        raise errors.ExecutorError("INVALID_CONFIGURATION", f"Expected Configuration|Platform: {selected}")
    return tuple(selected.split("|", 1))

def resolve_visual_studio_project_configurations(
    discovery: dict[str, Any],
    entry: str,
    projects: list[str],
    solution_configuration: str,
    solution_platform: str,
) -> list[dict[str, str]]:
    """Resolve .sln Configuration|Platform to each project without assuming identity mapping."""
    selected = f"{solution_configuration}|{solution_platform}"
    if not entry.casefold().endswith(".sln"):
        return [
            {
                "project": project,
                "configuration": solution_configuration,
                "platform": solution_platform,
                "source": "DIRECT_PROJECT",
            }
            for project in projects
        ]

    mappings = discovery["data"].get("solutionProjectConfigurations", {}).get(entry, {})
    resolved: list[dict[str, str]] = []
    for project in projects:
        project_mappings = mappings.get(project, {})
        mapping = project_mappings.get(selected)
        if mapping is None and not mappings:
            resolved.append(
                {
                    "project": project,
                    "configuration": solution_configuration,
                    "platform": solution_platform,
                    "source": "IDENTITY_FALLBACK_NO_SOLUTION_MAPPING",
                }
            )
            continue
        if mapping is None:
            continue
        if not mapping.get("build"):
            continue
        active = mapping.get("activeConfigurationPlatform")
        if not isinstance(active, str) or "|" not in active:
            raise WorkflowWaiting(
                "PROJECT_CONFIGURATION_UNRESOLVED",
                "The solution configuration does not provide one project Configuration|Platform mapping",
                {"configurationPlatform": [selected]},
                decision_owner="MODEL",
                purpose="BUILD_ENVIRONMENT",
                evidence=[entry, project],
            )
        configuration, platform = active.split("|", 1)
        resolved.append(
            {
                "project": project,
                "configuration": configuration,
                "platform": platform,
                "source": "SOLUTION_PROJECT_CONFIGURATION",
            }
        )
    if not resolved:
        raise WorkflowWaiting(
            "NO_PROJECTS_ENABLED_FOR_SOLUTION_CONFIGURATION",
            "No selected C/C++ project is enabled for the chosen solution Configuration|Platform",
            {"configurationPlatform": [selected]},
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[entry, *projects],
        )
    return resolved
