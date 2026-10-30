"""Project file classification and Visual Studio/CMake project parsing."""

from __future__ import annotations

import argparse
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import pathutil
from cover_build.core import errors


PROJECT_NAMES = {
    "cmakelists.txt": "CMAKE_PROJECT",
    "cmakepresets.json": "CMAKE_PRESETS",
    "cmakeuserpresets.json": "CMAKE_USER_PRESETS",
    "cmakecache.txt": "CMAKE_CACHE",
    "makefile": "MAKEFILE",
    "build.ninja": "NINJA_BUILD",
    "compile_commands.json": "COMPILE_COMMANDS",
    "azure-pipelines.yml": "CI_SCRIPT",
    "azure-pipelines.yaml": "CI_SCRIPT",
    ".gitlab-ci.yml": "CI_SCRIPT",
    ".gitlab-ci.yaml": "CI_SCRIPT",
    "jenkinsfile": "CI_SCRIPT",
    "configure": "BUILD_SCRIPT",
    "bootstrap": "BUILD_SCRIPT",
    ".project": "ECLIPSE_PROJECT",
    ".cproject": "ECLIPSE_CDT_PROJECT",
}


PROJECT_SUFFIXES = {
    ".sln": "VISUAL_STUDIO_SOLUTION",
    ".vcxproj": "VISUAL_CPP_PROJECT",
    ".vcproj": "VISUAL_CPP_LEGACY_PROJECT",
    ".csproj": "CSHARP_PROJECT",
    ".vbproj": "VISUAL_BASIC_PROJECT",
    ".fsproj": "FSHARP_PROJECT",
    ".props": "MSBUILD_PROPERTY_SHEET",
    ".targets": "MSBUILD_TARGETS",
    ".bat": "BUILD_SCRIPT",
    ".cmd": "BUILD_SCRIPT",
    ".ps1": "BUILD_SCRIPT",
    ".sh": "BUILD_SCRIPT",
    ".ioc": "STM32CUBE_IOC",
}


SKIP_DIRS = {".git", ".svn", ".hg", ".cover-build", "node_modules"}


ROOT_BUILD_SCRIPT_NAMES = {
    "build.bat",
    "build.cmd",
    "build.ps1",
    "build.sh",
    "configure",
    "bootstrap",
}


DEFERRED_INITIAL_TYPES = {"MSBUILD_PROPERTY_SHEET", "MSBUILD_TARGETS", "COMPILE_COMMANDS"}


def project_file_kind(root: Path, path: Path) -> str | None:
    name = path.name
    lower = name.lower()
    kind = PROJECT_NAMES.get(lower) or PROJECT_SUFFIXES.get(Path(lower).suffix)
    relative_parts = {part.casefold() for part in path.parent.relative_to(root).parts}
    if (
        kind is None
        and Path(lower).suffix in {".yml", ".yaml"}
        and {".github", "workflows"}.issubset(relative_parts)
    ):
        kind = "CI_SCRIPT"
    relative = path.relative_to(root)
    if kind == "BUILD_SCRIPT" and (len(relative.parts) != 1 or lower not in ROOT_BUILD_SCRIPT_NAMES):
        kind = None
    if kind in DEFERRED_INITIAL_TYPES:
        kind = None
    return kind


def walk_project_files(
    root: Path,
    index: dict[str, Any] | None = None,
) -> Iterable[tuple[Path, str]]:
    if index is not None:
        for item in index.get("entries", []):
            path = Path(item["path"])
            kind = project_file_kind(root, path)
            if kind:
                yield path, kind
        return
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name.lower() not in SKIP_DIRS]
        current = Path(current_root)
        for name in file_names:
            path = current / name
            kind = project_file_kind(root, path)
            if kind:
                yield path, kind


def parse_sln_configurations(path: Path) -> list[str]:
    results: list[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "GlobalSection(SolutionConfigurationPlatforms)" in line:
            inside = True
            continue
        if inside and "EndGlobalSection" in line:
            break
        if inside and "=" in line:
            value = line.split("=", 1)[0].strip()
            if value and value not in results:
                results.append(value)
    return results


def parse_vcxproj_configurations(path: Path) -> list[str]:
    results: list[str] = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise errors.ExecutorError("INVALID_VCXPROJ", f"Cannot parse {path}: {exc}") from exc
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "ProjectConfiguration":
            value = node.attrib.get("Include", "").strip()
            if value and value not in results:
                results.append(value)
    return results


SOLUTION_PROJECT_PATTERN = re.compile(
    r'^Project\("[^"]+"\)\s*=\s*"(?P<name>[^"]+)",\s*"(?P<path>[^"]+)",\s*"(?P<guid>\{[^}]+\})"',
    flags=re.IGNORECASE,
)


SOLUTION_PROJECT_CONFIGURATION_PATTERN = re.compile(
    r'^(?P<guid>\{[^}]+\})\.(?P<solution>.+)\.(?P<kind>ActiveCfg|Build\.\d+)\s*=\s*(?P<project>.+)$',
    flags=re.IGNORECASE,
)


C_SOURCE_SUFFIXES = {".c"}


CPP_SOURCE_SUFFIXES = {".cpp", ".cc", ".cxx", ".c++"}


def parse_solution_projects(path: Path) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    language_families = {
        ".vcxproj": "C_CPP",
        ".vcproj": "C_CPP_LEGACY",
        ".csproj": "CSHARP",
        ".vbproj": "VISUAL_BASIC",
        ".fsproj": "FSHARP",
    }
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        match = SOLUTION_PROJECT_PATTERN.match(raw_line.strip())
        if not match:
            continue
        relative = match.group("path")
        resolved = (path.parent / Path(relative.replace("\\", os.sep))).resolve(strict=False)
        results.append(
            {
                "name": match.group("name"),
                "relativePath": relative,
                "path": str(resolved),
                "projectGuid": match.group("guid"),
                "languageFamily": language_families.get(Path(relative).suffix.casefold(), "UNKNOWN"),
            }
        )
    return results


def parse_solution_project_configurations(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Return each solution configuration's evaluated project mapping from the .sln file."""
    project_paths = {
        item["projectGuid"].casefold(): item["path"]
        for item in parse_solution_projects(path)
    }
    results: dict[str, dict[str, dict[str, Any]]] = {}
    inside = False
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if "GlobalSection(ProjectConfigurationPlatforms)" in line:
            inside = True
            continue
        if inside and "EndGlobalSection" in line:
            break
        if not inside:
            continue
        match = SOLUTION_PROJECT_CONFIGURATION_PATTERN.match(line)
        if not match:
            continue
        project_path = project_paths.get(match.group("guid").casefold())
        if project_path is None:
            continue
        solution_configuration = match.group("solution").strip()
        project_configuration = match.group("project").strip()
        project_results = results.setdefault(project_path, {})
        mapping = project_results.setdefault(
            solution_configuration,
            {"activeConfigurationPlatform": None, "build": False},
        )
        if match.group("kind").casefold() == "activecfg":
            mapping["activeConfigurationPlatform"] = project_configuration
        else:
            mapping["build"] = True
            mapping.setdefault("buildConfigurationPlatform", project_configuration)
            if mapping["activeConfigurationPlatform"] is None:
                mapping["activeConfigurationPlatform"] = project_configuration
    return results


def parse_visual_cpp_language(path: Path, *, legacy: bool = False) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise errors.ExecutorError("INVALID_VISUAL_CPP_PROJECT", f"Cannot parse {path}: {exc}") from exc
    sources: list[str] = []
    for node in root.iter():
        local_name = node.tag.rsplit("}", 1)[-1]
        if legacy:
            value = node.attrib.get("RelativePath", "") if local_name == "File" else ""
        else:
            value = node.attrib.get("Include", "") if local_name == "ClCompile" else ""
        value = value.strip()
        if value and value not in sources:
            sources.append(value)
    c_sources = [value for value in sources if Path(value).suffix.casefold() in C_SOURCE_SUFFIXES]
    cpp_sources = [value for value in sources if Path(value).suffix.casefold() in CPP_SOURCE_SUFFIXES]
    if c_sources and cpp_sources:
        classification = "MIXED_C_CPP"
    elif c_sources:
        classification = "C"
    elif cpp_sources:
        classification = "CPP"
    else:
        classification = "UNKNOWN"
    return {
        "classification": classification,
        "languages": [name for values, name in ((c_sources, "C"), (cpp_sources, "CPP")) if values],
        "sourceCounts": {"C": len(c_sources), "CPP": len(cpp_sources)},
        "sources": sources,
    }


def parse_cmake_cache(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line or ":" not in line.split("=", 1)[0]:
            continue
        left, value = line.split("=", 1)
        key = left.split(":", 1)[0]
        if key.startswith("CMAKE_"):
            values[key] = value
    return values


def parse_cmake_cache_entries(path: Path) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line or ":" not in line.split("=", 1)[0]:
            continue
        left, value = line.split("=", 1)
        key, value_type = left.split(":", 1)
        if key and value_type:
            entries[key] = {"type": value_type, "value": value}
    return entries


def validate_cmake_cache_for_project(path: Path, values: dict[str, str], project_root: Path) -> dict[str, Any]:
    """Prove that a discovered cache still owns a build tree usable by cmake --build."""
    reasons: list[str] = []
    build_directory = path.parent
    home_value = values.get("CMAKE_HOME_DIRECTORY")
    home = Path(home_value).resolve(strict=False) if home_value else None
    if home is None or not home.is_dir() or not pathutil.path_within(home, project_root):
        reasons.append("CMAKE_HOME_DIRECTORY_OUTSIDE_OR_MISSING")
    elif not (home / "CMakeLists.txt").is_file():
        reasons.append("CMAKE_HOME_DIRECTORY_HAS_NO_CMAKELISTS")
    cache_directory = values.get("CMAKE_CACHEFILE_DIR")
    if cache_directory and not pathutil.same_path(cache_directory, build_directory):
        reasons.append("CMAKE_CACHEFILE_DIR_MISMATCH")
    cmake_command = values.get("CMAKE_COMMAND")
    if not cmake_command or not Path(cmake_command).is_file():
        reasons.append("CMAKE_COMMAND_MISSING")
    generator = values.get("CMAKE_GENERATOR", "").strip()
    if not generator:
        reasons.append("CMAKE_GENERATOR_MISSING")
    if not (build_directory / "CMakeFiles").is_dir():
        reasons.append("CMAKEFILES_DIRECTORY_MISSING")
    backend: Path | None = None
    generator_lower = generator.casefold()
    if "ninja" in generator_lower:
        backend = build_directory / "build.ninja"
    elif "makefiles" in generator_lower:
        backend = build_directory / "Makefile"
    elif "visual studio" in generator_lower:
        solutions = list(build_directory.glob("*.sln"))
        if not solutions:
            reasons.append("CMAKE_GENERATED_SOLUTION_MISSING")
    if backend is not None and not backend.is_file():
        reasons.append("CMAKE_GENERATED_BACKEND_MISSING")
    return {
        "validForProjectRoot": not reasons,
        "validationError": reasons[0] if reasons else None,
        "validationErrors": reasons,
        "sourceDirectory": str(home) if home is not None else None,
        "buildDirectory": str(build_directory.resolve()),
        "generator": generator or None,
    }


def discover_project(args: argparse.Namespace) -> dict[str, Any]:
    root = pathutil.canonical(args.root)
    if not root.is_dir():
        raise errors.ExecutorError("PROJECT_PATH_REQUIRED", f"Not a directory: {root}")
    files: list[dict[str, Any]] = []
    sln_configs: dict[str, list[str]] = {}
    vcx_configs: dict[str, list[str]] = {}
    solution_projects: dict[str, list[dict[str, str]]] = {}
    solution_project_configurations: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    project_languages: dict[str, dict[str, Any]] = {}
    caches: list[dict[str, Any]] = []
    index = getattr(args, "project_index", None)
    for path, kind in walk_project_files(root, index):
        record = {"path": str(path.resolve()), "type": kind}
        files.append(record)
        if kind == "VISUAL_STUDIO_SOLUTION":
            sln_configs[str(path.resolve())] = parse_sln_configurations(path)
            solution_projects[str(path.resolve())] = parse_solution_projects(path)
            solution_project_configurations[str(path.resolve())] = parse_solution_project_configurations(path)
        elif kind == "VISUAL_CPP_PROJECT":
            vcx_configs[str(path.resolve())] = parse_vcxproj_configurations(path)
            project_languages[str(path.resolve())] = parse_visual_cpp_language(path)
        elif kind == "VISUAL_CPP_LEGACY_PROJECT":
            project_languages[str(path.resolve())] = parse_visual_cpp_language(path, legacy=True)
        elif kind == "CMAKE_CACHE":
            values = parse_cmake_cache(path)
            validation = validate_cmake_cache_for_project(path, values, root)
            caches.append(
                {
                    "path": str(path.resolve()),
                    "values": values,
                    **validation,
                }
            )
    if not files:
        raise errors.ExecutorError("NO_PROJECT_FOUND", f"No supported project files under {root}")
    valid_cache_paths = {cache["path"] for cache in caches if cache["validForProjectRoot"]}
    cache_directories = {str(Path(path).parent).casefold() for path in valid_cache_paths}
    generated_backends: list[dict[str, str]] = []
    direct_makefiles: list[str] = []
    unsupported_ninja_files: list[str] = []
    for item in files:
        if item["type"] not in {"MAKEFILE", "NINJA_BUILD"}:
            continue
        parent_key = str(Path(item["path"]).parent).casefold()
        if parent_key in cache_directories:
            generated_backends.append(
                {
                    "path": item["path"],
                    "backend": "MAKE" if item["type"] == "MAKEFILE" else "NINJA",
                    "owner": "CMAKE_CACHE",
                }
            )
        elif item["type"] == "MAKEFILE":
            direct_makefiles.append(item["path"])
        else:
            unsupported_ninja_files.append(item["path"])
    has_vs = any(item["type"] in {"VISUAL_STUDIO_SOLUTION", "VISUAL_CPP_PROJECT", "VISUAL_CPP_LEGACY_PROJECT"} for item in files)
    has_cmake = any(item["type"] in {"CMAKE_PROJECT", "CMAKE_PRESETS"} for item in files) or bool(valid_cache_paths)
    has_make = bool(direct_makefiles)
    candidate_types = [name for flag, name in ((has_vs, "VISUAL_STUDIO"), (has_cmake, "CMAKE"), (has_make, "MAKE")) if flag]
    selection = candidate_types[0] if len(candidate_types) == 1 else "AMBIGUOUS" if candidate_types else "UNSUPPORTED"
    model_reasons: list[str] = []
    if len(candidate_types) > 1:
        model_reasons.append("MULTIPLE_BUILD_SYSTEMS")
    if not candidate_types and not unsupported_ninja_files:
        model_reasons.append("NO_SUPPORTED_BUILD_SYSTEM")
    if any(item["type"] == "VISUAL_CPP_LEGACY_PROJECT" for item in files):
        model_reasons.append("LEGACY_VCPROJ_REQUIRES_REVIEW")
    for projects in solution_projects.values():
        families = {item["languageFamily"] for item in projects if item["languageFamily"] != "UNKNOWN"}
        if len(families) > 1:
            model_reasons.append("MULTI_LANGUAGE_SOLUTION")
            break
    return envelopes.envelope(
        "ANALYZING_PROJECTS",
        "WAITING_MODEL" if model_reasons else "SUCCEEDED",
        {
            "projectRoot": str(root),
            "projectIndex": {
                "schema": index.get("schema"),
                "scannedFileCount": index.get("scannedFileCount"),
            } if isinstance(index, dict) else None,
            "sourceRoles": sorted(
                role for role in ("C", "CXX")
                if isinstance(index, dict) and index.get("sourceCounts", {}).get(role, 0) > 0
            ),
            "artifactCandidates": list(index.get("artifactCandidates", [])) if isinstance(index, dict) else [],
            "files": sorted(files, key=lambda item: item["path"].lower()),
            "candidateBuildSystems": candidate_types,
            "selectedBuildSystem": selection,
            "solutionConfigurations": sln_configs,
            "projectConfigurations": vcx_configs,
            "solutionProjects": solution_projects,
            "solutionProjectConfigurations": solution_project_configurations,
            "projectLanguages": project_languages,
            "modelReviewReasons": model_reasons,
            "cmakeCaches": caches,
            "gccBuildDrivers": {
                "CMAKE": [
                    item["path"]
                    for item in files
                    if item["type"] in {"CMAKE_PROJECT", "CMAKE_PRESETS"}
                    or (item["type"] == "CMAKE_CACHE" and item["path"] in valid_cache_paths)
                ],
                "MAKE": sorted(direct_makefiles, key=str.casefold),
            },
            "generatedBackends": sorted(generated_backends, key=lambda item: item["path"].casefold()),
            "unsupportedBuildSystems": ["NINJA"] if unsupported_ninja_files else [],
            "unsupportedBuildFiles": sorted(unsupported_ninja_files, key=str.casefold),
        },
    )


def find_single(paths: list[Path], code: str, description: str) -> Path:
    if len(paths) != 1:
        raise errors.ExecutorError(code, f"Expected exactly one {description}; found {len(paths)}", [str(path) for path in paths])
    return paths[0]
