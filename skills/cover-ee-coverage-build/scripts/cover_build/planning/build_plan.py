"""Collect build evidence and validate typed original-build plans."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from cover_build.common import fileio
from cover_build.common import gcc_tools
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.common import project_files
from cover_build.core import errors
import os


BUILD_EVIDENCE_SCHEMA = "cover-build-evidence/v1"

ORIGINAL_BUILD_PLAN_SCHEMA = "cover-original-build-plan/v1"

ORIGINAL_BUILD_PLAN_ORIGINS = {
    "SCRIPT_EXTRACTED",
    "MODEL_INFERRED",
    "USER_CONFIRMED_FALLBACK",
}

COMMAND_PHASES = {"configure", "commands", "restore", "clean", "rebuild", "build"}

GCC_DRIVERS = {"CMAKE", "MAKE"}

MANAGED_DRIVERS = {
    "CSHARP_MSBUILD",
    "CSHARP_DOTNET",
    "JAVA_MAVEN",
    "JAVA_GRADLE",
    "JAVA_CLI",
}

SUPPORTED_DRIVERS = {*GCC_DRIVERS, *MANAGED_DRIVERS, "MSBUILD", "STM32CUBEIDE"}

GCC_ROLES = {"c_compiler", "cxx_compiler", "ar", "linker"}

MSBUILD_ROLES = {"compiler", "linker", "archiver"}

DRIVER_ROLES = {
    "CMAKE": GCC_ROLES,
    "MAKE": GCC_ROLES,
    "STM32CUBEIDE": GCC_ROLES,
    "MSBUILD": MSBUILD_ROLES,
    "CSHARP_MSBUILD": set(),
    "CSHARP_DOTNET": set(),
    "JAVA_MAVEN": set(),
    "JAVA_GRADLE": set(),
    "JAVA_CLI": set(),
}

INJECTION_STRATEGIES = {
    "CMAKE": "CMAKE_COMPILER_OVERRIDE",
    "MAKE": "MAKE_VARIABLE_OVERRIDE",
    "MSBUILD": "MSBUILD_EXECUTABLE_PATH_OVERLAY",
    "STM32CUBEIDE": "STM32CUBEIDE_HEADLESS_PATH_PREPEND",
    "CSHARP_MSBUILD": "CSHARP_TOOLBOX",
    "CSHARP_DOTNET": "CSHARP_TOOLBOX",
    "JAVA_MAVEN": "JACOV_PLUGIN",
    "JAVA_GRADLE": "JACOV_PLUGIN",
    "JAVA_CLI": "JACOV_CLI",
}

WRAPPER_PLACEHOLDER = re.compile(r"\$\{COVER_WRAPPER:([a-z_]+)\}")

SENSITIVE_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|API_?KEY|PRIVATE_?KEY|AUTHORIZATION)",
    flags=re.IGNORECASE,
)

LIKELY_BUILD_SCRIPT = re.compile(r"(?:build|rebuild|compile|configure|bootstrap|setup|make)", flags=re.IGNORECASE)

SAFE_CMAKE_CACHE_KEYS = {
    "CMAKE_HOME_DIRECTORY",
    "CMAKE_GENERATOR",
    "CMAKE_BUILD_TYPE",
    "CMAKE_COMMAND",
    "CMAKE_C_COMPILER",
    "CMAKE_CXX_COMPILER",
    "CMAKE_AR",
    "CMAKE_LINKER",
    "CMAKE_MAKE_PROGRAM",
    "CMAKE_TOOLCHAIN_FILE",
    "CMAKE_SYSROOT",
}

ORIGINAL_BUILD_EVIDENCE_SOURCES = {
    "USER_PROVIDED",
    "SCRIPT",
    "CI",
    "CACHE",
    "PRESET",
    "MODEL",
}

ENVIRONMENT_INITIALIZATION_METHODS = {
    "INHERITED_PROCESS",
    "VSDEVCMD_CAPTURED",
    "CUSTOMER_BUILD_SCRIPT",
    "SDK_ENVIRONMENT_SCRIPT",
    "STRUCTURED_ENVIRONMENT_DELTA",
}

STANDARD_MAKE_TOOL_VARIABLES = {
    "c_compiler": "CC",
    "cxx_compiler": "CXX",
    "ar": "AR",
    "linker": "LD",
}

MAKE_TOOL_NAMES = {
    "gcc": "c_compiler", "cc": "c_compiler", "clang": "c_compiler", "arm-none-eabi-gcc": "c_compiler",
    "g++": "cxx_compiler", "c++": "cxx_compiler", "clang++": "cxx_compiler", "arm-none-eabi-g++": "cxx_compiler",
    "ar": "ar", "gcc-ar": "ar", "llvm-ar": "ar", "arm-none-eabi-ar": "ar",
    "ld": "linker", "ld.lld": "linker", "arm-none-eabi-ld": "linker",
}

def _job_directory(job: dict[str, Any]) -> Path:
    value = job.get("jobDirectory")
    if value:
        return pathutil.canonical(value, False)
    return pathutil.canonical(Path(job["directories"]["temporary"]).parent, False)

def _file_record(path: Path, kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "type": kind, "exists": path.is_file()}
    if path.is_file():
        stat = path.stat()
        result.update({"size": stat.st_size, "modifiedNs": stat.st_mtime_ns, "sha256": pathutil.sha256_file(path)})
    return result

def _cache_record(cache: dict[str, Any]) -> dict[str, Any]:
    values = cache.get("values") if isinstance(cache.get("values"), dict) else {}
    safe_values = {
        key: value
        for key, value in values.items()
        if key in SAFE_CMAKE_CACHE_KEYS and not SENSITIVE_NAME.search(key) and isinstance(value, str)
    }
    return {
        "path": cache.get("path"),
        "validForProjectRoot": cache.get("validForProjectRoot") is True,
        "validationError": cache.get("validationError"),
        "values": safe_values,
    }

def _preset_records(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "configurePresets": [], "buildPresets": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result["parseError"] = f"{type(exc).__name__}: {exc}"
        return result
    if not isinstance(value, dict):
        result["parseError"] = "Preset root must be an object"
        return result
    for key, output_key in (("configurePresets", "configurePresets"), ("buildPresets", "buildPresets")):
        presets = value.get(key, [])
        if not isinstance(presets, list):
            result["parseError"] = f"{key} must be an array"
            continue
        for preset in presets:
            if not isinstance(preset, dict) or not isinstance(preset.get("name"), str):
                continue
            record = {
                name: preset[name]
                for name in (
                    "name",
                    "hidden",
                    "inherits",
                    "generator",
                    "binaryDir",
                    "configurePreset",
                    "configuration",
                    "targets",
                )
                if name in preset
            }
            environment = preset.get("environment")
            if isinstance(environment, dict):
                record["environmentNames"] = sorted(str(name) for name in environment)
                record["sensitiveEnvironmentNames"] = sorted(
                    str(name) for name in environment if SENSITIVE_NAME.search(str(name))
                )
            result[output_key].append(record)
    return result

def _stm32cubeide_record(discovery: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(discovery, dict):
        return None
    return {
        "schema": discovery.get("schema"),
        "status": discovery.get("status"),
        "confidence": discovery.get("confidence"),
        "routingRecommendation": discovery.get("routingRecommendation"),
        "projects": list(discovery.get("projects") or []),
        "candidateEvidence": list(discovery.get("candidateEvidence") or []),
        "expectedToolRoles": list(discovery.get("expectedToolRoles") or []),
    }

def _existing_build_logs(job: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect only known, shallow build-log locations; never scan arbitrary logs."""
    project_root = pathutil.canonical(job["projectRoot"], False)
    candidates = [
        project_root / name
        for name in ("build.log", "msbuild.log", "build.binlog", "msbuild.binlog")
    ]
    for cache in data.get("cmakeCaches") or []:
        raw_path = cache.get("path") if isinstance(cache, dict) else None
        if not isinstance(raw_path, str):
            continue
        cmake_files = pathutil.canonical(raw_path, False).parent / "CMakeFiles"
        candidates.extend(
            cmake_files / name
            for name in ("CMakeConfigureLog.yaml", "CMakeOutput.log", "CMakeError.log")
        )
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = pathutil.canonical(candidate)
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        record = _file_record(resolved, "EXISTING_BUILD_LOG")
        try:
            record["relativePath"] = str(resolved.relative_to(project_root))
        except ValueError:
            record["relativePath"] = None
        records.append(record)
    return sorted(records, key=lambda value: value["path"].casefold())

def _make_tool_role(value: str) -> str | None:
    token = value.strip().split(maxsplit=1)[0].strip('"\'') if value.strip() else ""
    name = Path(token.replace("\\", "/")).name.casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    exact = MAKE_TOOL_NAMES.get(name)
    if exact:
        return exact
    for suffix, role in (
        ("g++", "cxx_compiler"), ("clang++", "cxx_compiler"),
        ("gcc", "c_compiler"), ("clang", "c_compiler"),
        ("gcc-ar", "ar"), ("llvm-ar", "ar"), ("ar", "ar"),
        ("ld.lld", "linker"), ("ld", "linker"),
    ):
        if name.endswith(f"-{suffix}"):
            return role
    return None

def _make_injection_record(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[: 1024 * 1024]
    except OSError as exc:
        return {"path": str(path), "status": "MODEL_REQUIRED", "parseError": f"{type(exc).__name__}: {exc}"}
    assignments: dict[str, str] = {}
    recipe_variables: set[str] = set()
    hardcoded_roles: set[str] = set()
    for raw in text.splitlines():
        assignment = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|\?=|\+=|=)\s*(.*?)\s*$", raw)
        if assignment and not SENSITIVE_NAME.search(assignment.group(1)):
            assignments[assignment.group(1)] = assignment.group(2)
        if not raw.startswith("\t"):
            continue
        recipe_variables.update(re.findall(r"\$\(([^)]+)\)|\$\{([^}]+)\}", raw))
        command = raw.lstrip()[1:].lstrip() if raw.lstrip().startswith(("@", "-", "+")) else raw.lstrip()
        role = _make_tool_role(command)
        if role:
            hardcoded_roles.add(role)
    flattened_variables = {
        item
        for groups in recipe_variables
        for item in groups
        if item and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) and not SENSITIVE_NAME.search(item)
    }
    candidates: dict[str, list[str]] = {}
    for variable in sorted(flattened_variables):
        role = _make_tool_role(assignments.get(variable, ""))
        if role:
            candidates.setdefault(role, []).append(variable)
    nonstandard = {
        role: names
        for role, names in candidates.items()
        if names != [STANDARD_MAKE_TOOL_VARIABLES[role]]
    }
    status = "MODEL_REQUIRED" if hardcoded_roles or nonstandard else "DEFAULT_VARIABLES"
    return {
        "path": str(path),
        "status": status,
        "toolVariableCandidates": candidates,
        "nonstandardToolVariables": nonstandard,
        "hardcodedToolRoles": sorted(hardcoded_roles),
    }

def collect_build_evidence(
    job: dict[str, Any],
    discovery: dict[str, Any],
    stm32cubeide_discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a secret-free, integrity-protected input for model planning."""
    data = discovery.get("data", discovery)
    files: list[dict[str, Any]] = []
    presets: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] = []
    make_injection: list[dict[str, Any]] = []
    for item in data.get("files", []):
        path = pathutil.canonical(item["path"], False)
        kind = str(item.get("type") or "UNKNOWN")
        record = _file_record(path, kind)
        try:
            relative = path.relative_to(pathutil.canonical(job["projectRoot"], False))
            record["relativePath"] = str(relative)
            record["depth"] = len(relative.parts)
        except ValueError:
            record["relativePath"] = None
            record["depth"] = None
        files.append(record)
        if kind in {"CMAKE_PRESETS", "CMAKE_USER_PRESETS"} and path.is_file():
            presets.append(_preset_records(path))
        if kind == "MAKEFILE" and path.is_file():
            make_injection.append(_make_injection_record(path))
        if kind in {"BUILD_SCRIPT", "CI_SCRIPT"}:
            parents = {part.casefold() for part in Path(record.get("relativePath") or "").parts[:-1]}
            record["likelyBuildEntry"] = bool(
                (isinstance(record.get("depth"), int) and record["depth"] <= 2 and LIKELY_BUILD_SCRIPT.search(path.stem))
                or parents.intersection({"ci", ".github", "build", "scripts"})
            )
            scripts.append(record)
    evidence = fileio.add_integrity(
        {
            "schema": BUILD_EVIDENCE_SCHEMA,
            "jobId": job["jobId"],
            "projectRoot": job["projectRoot"],
            "candidateBuildSystems": list(data.get("candidateBuildSystems") or []),
            "solutionProjects": dict(data.get("solutionProjects") or {}),
            "solutionProjectConfigurations": dict(data.get("solutionProjectConfigurations") or {}),
            "projectLanguages": dict(data.get("projectLanguages") or {}),
            "modelReviewReasons": list(data.get("modelReviewReasons") or []),
            "files": sorted(files, key=lambda value: value["path"].casefold()),
            "buildScripts": sorted(scripts, key=lambda value: value["path"].casefold()),
            "cmakePresets": presets,
            "cmakeCaches": [_cache_record(cache) for cache in data.get("cmakeCaches") or []],
            "existingBuildLogs": _existing_build_logs(job, data),
            "generatedBackends": list(data.get("generatedBackends") or []),
            "makeInjection": sorted(make_injection, key=lambda value: value["path"].casefold()),
            "stm32CubeIDE": _stm32cubeide_record(stm32cubeide_discovery),
            "policy": {
                "secretsPersisted": False,
                "modelRole": "INTERPRET_AND_PLAN",
                "scriptRole": "COLLECT_VALIDATE_EXECUTE_VERIFY",
            },
        }
    )
    return evidence

def collect_selected_references(
    job: dict[str, Any],
    entries: list[str],
    *,
    max_depth: int = 8,
    max_files: int = 256,
) -> dict[str, Any]:
    """Follow only literal references owned by selected build entries."""
    root = pathutil.canonical(job["projectRoot"])
    queue: list[tuple[Path, int, str]] = []
    unresolved: list[dict[str, str]] = []
    for value in entries:
        path = pathutil.canonical(value, False)
        if path.is_file() and pathutil.path_within(path, root):
            queue.append((path, 0, "SELECTED_ENTRY"))
        else:
            unresolved.append({"source": str(path), "reference": str(value), "reason": "ENTRY_OUTSIDE_OR_MISSING"})
    visited: dict[str, dict[str, Any]] = {}

    def add_reference(source: Path, raw: str, depth: int, relation: str, *, cmake_directory: bool = False) -> None:
        value = raw.strip().strip('"\'')
        value = value.replace("$(MSBuildThisFileDirectory)", f"{source.parent}{os.sep}")
        value = value.replace("$(MSBuildProjectDirectory)", str(source.parent))
        if not value or "$(" in value or "${" in value or "$<" in value:
            unresolved.append({"source": str(source), "reference": raw, "reason": "DYNAMIC_REFERENCE"})
            return
        candidate = Path(value.replace("\\", os.sep))
        candidate = candidate if candidate.is_absolute() else source.parent / candidate
        if cmake_directory:
            candidate = candidate / "CMakeLists.txt"
        elif not candidate.suffix and source.name.casefold() in {"cmakelists.txt"}:
            cmake_file = candidate.with_suffix(".cmake")
            if cmake_file.is_file():
                candidate = cmake_file
        candidate = candidate.resolve(strict=False)
        if not pathutil.path_within(candidate, root):
            unresolved.append({"source": str(source), "reference": raw, "reason": "REFERENCE_OUTSIDE_PROJECT"})
        elif not candidate.is_file():
            unresolved.append({"source": str(source), "reference": raw, "reason": "REFERENCE_MISSING"})
        elif depth <= max_depth:
            queue.append((candidate, depth, relation))

    while queue and len(visited) < max_files:
        path, depth, relation = queue.pop(0)
        key = str(path.resolve()).casefold()
        if key in visited:
            continue
        visited[key] = {**_file_record(path, relation), "depth": depth}
        if depth >= max_depth:
            continue
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        if suffix == ".sln":
            for project in project_files.parse_solution_projects(path):
                add_reference(path, project["relativePath"], depth + 1, "SOLUTION_PROJECT")
        elif suffix in {".vcxproj", ".props", ".targets"}:
            try:
                xml_root = ET.parse(path).getroot()
            except ET.ParseError as exc:
                unresolved.append({"source": str(path), "reference": "", "reason": f"XML_PARSE_ERROR:{exc}"})
                continue
            for node in xml_root.iter():
                local = node.tag.rsplit("}", 1)[-1]
                if local == "Import" and node.attrib.get("Project"):
                    add_reference(path, node.attrib["Project"], depth + 1, "MSBUILD_IMPORT")
                elif local == "ProjectReference" and node.attrib.get("Include"):
                    add_reference(path, node.attrib["Include"], depth + 1, "MSBUILD_PROJECT_REFERENCE")
            current = path.parent
            while pathutil.path_within(current, root):
                for directory_file in ("Directory.Build.props", "Directory.Build.targets"):
                    candidate = current / directory_file
                    if candidate.is_file():
                        queue.append((candidate, depth + 1, "MSBUILD_DIRECTORY_BUILD"))
                if pathutil.same_path(current, root):
                    break
                current = current.parent
        elif name == "cmakelists.txt" or suffix == ".cmake":
            text = path.read_text(encoding="utf-8-sig", errors="replace")[: 2 * 1024 * 1024]
            for match in re.finditer(r"(?i)\badd_subdirectory\s*\(\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s\)]+))", text):
                add_reference(path, next(value for value in match.groups() if value is not None), depth + 1, "CMAKE_SUBDIRECTORY", cmake_directory=True)
            for match in re.finditer(r"(?i)\binclude\s*\(\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s\)]+))", text):
                add_reference(path, next(value for value in match.groups() if value is not None), depth + 1, "CMAKE_INCLUDE")
        elif name in {"makefile", "gnumakefile"} or suffix in {".mk", ".mak"}:
            text = path.read_text(encoding="utf-8-sig", errors="replace")[: 2 * 1024 * 1024]
            for raw_line in text.splitlines():
                match = re.match(r"^\s*(?:-?include|sinclude)\s+(.+?)\s*$", raw_line)
                if not match:
                    continue
                for value in match.group(1).split():
                    add_reference(path, value, depth + 1, "MAKE_INCLUDE")
    truncated = bool(queue)
    return fileio.add_integrity(
        {
            "schema": "cover-selected-build-references/v1",
            "jobId": job["jobId"],
            "projectRoot": str(root),
            "entries": [str(pathutil.canonical(value, False)) for value in entries],
            "files": sorted(visited.values(), key=lambda value: value["path"].casefold()),
            "unresolved": unresolved,
            "truncated": truncated,
            "limits": {"maxDepth": max_depth, "maxFiles": max_files},
        }
    )


def selected_references_fingerprint(value: dict[str, Any]) -> str:
    """Hash selected build inputs without timestamp-only restore churn."""
    files = []
    for item in value.get("files") or []:
        if not isinstance(item, dict):
            continue
        record = {
            key: item.get(key)
            for key in ("path", "type", "exists", "size", "sha256", "depth")
        }
        if not item.get("sha256"):
            record["modifiedNs"] = item.get("modifiedNs")
        files.append(record)
    payload = {
        "schema": value.get("schema"),
        "projectRoot": value.get("projectRoot"),
        "entries": value.get("entries"),
        "files": files,
        "unresolved": value.get("unresolved"),
        "truncated": value.get("truncated"),
        "limits": value.get("limits"),
    }
    return fileio.sha256_json(payload)


def requires_model_original_plan(evidence: dict[str, Any], driver: str, adapter_entry: str | None = None) -> bool:
    """Use the model only when deterministic evidence does not define one safe entry."""
    if driver not in {*GCC_DRIVERS, "MSBUILD", "STM32CUBEIDE"}:
        return True
    if any(script.get("likelyBuildEntry") for script in evidence.get("buildScripts", [])):
        return True
    if driver == "CMAKE":
        valid_caches = [cache for cache in evidence.get("cmakeCaches", []) if cache.get("validForProjectRoot")]
        visible_presets = [
            preset
            for file_record in evidence.get("cmakePresets", [])
            for preset in file_record.get("configurePresets", [])
            if preset.get("hidden") is not True
        ]
        return bool(visible_presets and not valid_caches)
    if driver == "MAKE":
        records = list(evidence.get("makeInjection") or [])
        if adapter_entry is not None:
            records = [record for record in records if pathutil.same_path(record["path"], adapter_entry)]
        return any(record.get("status") == "MODEL_REQUIRED" for record in records)
    return False

def create_original_build_plan(
    job: dict[str, Any],
    driver: str,
    build_tool: str,
    adapter_entry: str,
    commands: dict[str, dict[str, Any]],
    original_build_directory: str,
    required_roles: list[str],
    evidence: list[str],
    *,
    origin: str = "SCRIPT_EXTRACTED",
    evidence_source: str = "SCRIPT",
    environment_initialization: dict[str, Any] | None = None,
    generator: str | None = None,
    configuration: str | None = None,
    coverage_injection: dict[str, Any] | None = None,
    target_tools: dict[str, str] | None = None,
    targets: list[str] | None = None,
) -> dict[str, Any]:
    strategy = INJECTION_STRATEGIES[driver]
    plan = {
        "schema": ORIGINAL_BUILD_PLAN_SCHEMA,
        "jobId": job["jobId"],
        "origin": origin,
        "evidenceSource": evidence_source,
        "environmentInitialization": environment_initialization or {"method": "INHERITED_PROCESS"},
        "driver": driver,
        "buildTool": build_tool,
        "adapterEntry": adapter_entry,
        "originalBuildDirectory": original_build_directory,
        "generator": generator,
        "configuration": configuration,
        "targets": targets or [],
        "requiredRoles": required_roles,
        "commands": commands,
        "coverageInjection": coverage_injection or {"strategy": strategy},
        "targetTools": target_tools or {},
        "evidence": evidence,
    }
    return validate_original_build_plan(job, plan, driver)

def _validate_command_spec(job: dict[str, Any], phase: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"commands.{phase} must be an object")
    executable = pathutil.canonical(str(raw.get("executable") or ""))
    if not executable.is_file():
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"commands.{phase}.executable is not a file")
    working_directory = pathutil.canonical(str(raw.get("workingDirectory") or ""))
    if not working_directory.is_dir() or not (
        pathutil.path_within(working_directory, job["projectRoot"])
        or pathutil.path_within(working_directory, _job_directory(job))
    ):
        raise errors.ExecutorError(
            "INVALID_ORIGINAL_BUILD_PLAN",
            f"commands.{phase}.workingDirectory must be under projectRoot or the current job",
            [str(working_directory)],
        )
    arguments = raw.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(value, str) and "\0" not in value for value in arguments):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"commands.{phase}.arguments must be a string array")
    if any(SENSITIVE_NAME.search(value) for value in arguments):
        raise errors.ExecutorError(
            "SENSITIVE_OR_INVALID_BUILD_COMMAND",
            f"commands.{phase}.arguments contains a credential-like value",
        )
    environment = raw.get("environmentDelta", {})
    if not isinstance(environment, dict):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"commands.{phase}.environmentDelta must be an object")
    for name, value in environment.items():
        if not isinstance(name, str) or not isinstance(value, str) or SENSITIVE_NAME.search(name):
            raise errors.ExecutorError(
                "SENSITIVE_OR_INVALID_BUILD_ENVIRONMENT",
                f"commands.{phase}.environmentDelta contains an invalid or sensitive entry",
                [str(name)],
            )
    timeout = raw.get("timeoutSeconds", 3600)
    if not isinstance(timeout, int) or not 1 <= timeout <= 86400:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"commands.{phase}.timeoutSeconds is out of range")
    return processes.command_spec(executable, list(arguments), working_directory, timeout) | {"environmentDelta": dict(environment)}

def _default_evidence_source(origin: str) -> str:
    if origin == "USER_CONFIRMED_FALLBACK":
        return "USER_PROVIDED"
    if origin == "SCRIPT_EXTRACTED":
        return "SCRIPT"
    return "MODEL"

def _validate_environment_initialization(raw: Any) -> dict[str, str]:
    """Accept setup provenance, never a shell setup command or environment values."""
    if raw is None:
        return {"method": "INHERITED_PROCESS"}
    if not isinstance(raw, dict):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "environmentInitialization must be an object")
    method = raw.get("method")
    if method not in ENVIRONMENT_INITIALIZATION_METHODS:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "environmentInitialization.method is invalid")
    script_path = raw.get("scriptPath")
    if script_path is not None:
        if not isinstance(script_path, str) or not script_path or "\0" in script_path:
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "environmentInitialization.scriptPath is invalid")
        script = pathutil.canonical(script_path)
        if not script.is_file():
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "environmentInitialization.scriptPath is not a file")
        return {"method": method, "scriptPath": str(script)}
    return {"method": method}

def validate_original_build_plan(job: dict[str, Any], raw: Any, expected_driver: str) -> dict[str, Any]:
    """Fail closed before executing a model- or user-authored original build plan."""
    if not isinstance(raw, dict) or raw.get("schema") != ORIGINAL_BUILD_PLAN_SCHEMA:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"Expected {ORIGINAL_BUILD_PLAN_SCHEMA}")
    if raw.get("jobId") != job["jobId"]:
        raise errors.ExecutorError("ORIGINAL_BUILD_PLAN_JOB_MISMATCH", "Original build plan belongs to another job")
    driver = raw.get("driver")
    if driver == "NINJA" or expected_driver == "NINJA":
        raise errors.ExecutorError(
            "NINJA_BUILD_UNSUPPORTED",
            "A direct Ninja plan is unsupported; provide the owning CMake entry or a direct Make entry",
        )
    if driver != expected_driver or driver not in SUPPORTED_DRIVERS:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "Original build plan driver does not match the selected adapter")
    origin = raw.get("origin")
    if origin not in ORIGINAL_BUILD_PLAN_ORIGINS:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "Original build plan has no valid origin tier")
    evidence_source = raw.get("evidenceSource", _default_evidence_source(origin))
    if evidence_source not in ORIGINAL_BUILD_EVIDENCE_SOURCES:
        raise errors.ExecutorError(
            "INVALID_ORIGINAL_BUILD_PLAN",
            "Original build plan has no valid evidence source",
        )
    environment_initialization = _validate_environment_initialization(raw.get("environmentInitialization"))
    build_tool = pathutil.canonical(str(raw.get("buildTool") or ""))
    adapter_entry = pathutil.canonical(str(raw.get("adapterEntry") or ""))
    if not build_tool.is_file() or not adapter_entry.is_file():
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "buildTool and adapterEntry must be files")
    if not pathutil.path_within(adapter_entry, job["projectRoot"]):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "adapterEntry must be under projectRoot")
    original_directory = pathutil.canonical(str(raw.get("originalBuildDirectory") or ""), False)
    if not (
        pathutil.path_within(original_directory, job["projectRoot"])
        or pathutil.path_within(original_directory, _job_directory(job))
    ):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "originalBuildDirectory is outside the allowed scope")
    commands = raw.get("commands")
    required_phases = {
        "MSBUILD": {"build"},
        "CMAKE": {"configure", "rebuild"},
        "STM32CUBEIDE": {"build"},
        "CSHARP_MSBUILD": {"build"},
        "CSHARP_DOTNET": {"build"},
        "JAVA_MAVEN": {"build"},
        "JAVA_GRADLE": {"build"},
        "JAVA_CLI": {"build"},
    }.get(driver)
    clean_build = isinstance(commands, dict) and {"clean", "build"}.issubset(commands)
    uses_rebuild_command = isinstance(commands, dict) and "rebuild" in commands
    supports_rebuild_choice = driver == "MAKE"
    commands_valid = (
        isinstance(commands, dict)
        and (
            required_phases.issubset(commands)
            if required_phases is not None
            else supports_rebuild_choice and clean_build != uses_rebuild_command
        )
    )
    if not commands_valid:
        expected = sorted(required_phases) if required_phases is not None else ["clean+build", "rebuild"]
        raise errors.ExecutorError(
            "INVALID_ORIGINAL_BUILD_PLAN",
            f"{driver} original build plan requires {expected} commands",
        )
    unknown_phases = sorted(set(commands) - COMMAND_PHASES)
    if unknown_phases:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "Original build plan contains unknown phases", unknown_phases)
    normalized_commands = {phase: _validate_command_spec(job, phase, spec) for phase, spec in commands.items()}
    targets = raw.get("targets", [])
    if not isinstance(targets, list) or not all(isinstance(target, str) and target and "\0" not in target for target in targets):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "targets must be a string array")
    allowed_roles = DRIVER_ROLES[driver]
    required_roles = raw.get("requiredRoles")
    managed_driver = driver in MANAGED_DRIVERS
    if not isinstance(required_roles, list) or not all(role in allowed_roles for role in required_roles):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"requiredRoles must contain known {driver} roles")
    if managed_driver and required_roles:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"{driver} must not declare native tool roles")
    if not managed_driver and not required_roles:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", f"requiredRoles must contain known {driver} roles")
    target_tools = raw.get("targetTools", {})
    if not isinstance(target_tools, dict):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "targetTools must be an object")
    normalized_target_tools: dict[str, str] = {}
    for role, path in target_tools.items():
        if role not in allowed_roles or role not in required_roles or not isinstance(path, str):
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "targetTools contains an invalid role", [{role: path}])
        resolved = pathutil.canonical(path)
        if not resolved.is_file():
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "targetTools values must be executable files", [path])
        normalized_target_tools[role] = str(resolved)
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(value, str) and value for value in evidence):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "Model plans must cite the evidence used")
    if any(SENSITIVE_NAME.search(value) for value in evidence):
        raise errors.ExecutorError("SENSITIVE_OR_INVALID_BUILD_COMMAND", "Build-plan evidence contains a credential-like value")
    injection = raw.get("coverageInjection")
    expected_strategy = INJECTION_STRATEGIES[driver]
    if not isinstance(injection, dict) or injection.get("strategy") != expected_strategy:
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", f"{driver} requires {expected_strategy}")
    tool_variables = injection.get("toolVariables", {})
    if not isinstance(tool_variables, dict):
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "toolVariables must be an object")
    for role, variable in tool_variables.items():
        if role not in GCC_ROLES or not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "Invalid Make tool-variable mapping", [{role: variable}])
    if driver != "MAKE" and tool_variables:
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "toolVariables are supported only by direct Make")
    configure_arguments = injection.get("configureArguments", [])
    if not isinstance(configure_arguments, list) or not all(isinstance(value, str) for value in configure_arguments):
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "configureArguments must be a string array")
    if driver == "CMAKE":
        gcc_tools.validate_cmake_configure_args(configure_arguments)
    elif configure_arguments:
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "configureArguments are supported only by CMake")
    force_rebuild = injection.get("forceRebuild", driver == "MAKE" and uses_rebuild_command)
    if not isinstance(force_rebuild, bool):
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "forceRebuild must be a boolean")
    clean_target = injection.get(
        "cleanTarget",
        None if managed_driver or (driver == "MAKE" and force_rebuild) else "clean",
    )
    if driver == "MAKE" and force_rebuild:
        if clean_target not in {None, ""}:
            raise errors.ExecutorError(
                "INVALID_COVERAGE_INJECTION",
                "forceRebuild cannot be combined with cleanTarget",
            )
        clean_target = None
    elif managed_driver and clean_target is None:
        pass
    elif not isinstance(clean_target, str) or not clean_target or "\0" in clean_target:
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "cleanTarget must be a non-empty string")
    if driver != "MAKE" and force_rebuild:
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "forceRebuild is supported only by direct Make")
    if driver == "MAKE" and force_rebuild != uses_rebuild_command:
        raise errors.ExecutorError(
            "INVALID_COVERAGE_INJECTION",
            "Make original commands and forceRebuild must select the same rebuild mode",
        )
    tool_wrappers = injection.get("toolWrappers", {})
    if not isinstance(tool_wrappers, dict):
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "toolWrappers must be an object")
    for role, wrapper in tool_wrappers.items():
        if role not in allowed_roles or role not in required_roles or not isinstance(wrapper, str) or Path(wrapper).name != wrapper:
            raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "Invalid wrapper-name mapping", [{role: wrapper}])
    coverage_commands = injection.get("coverageCommands", {})
    if not isinstance(coverage_commands, dict):
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "coverageCommands must be an object")
    if coverage_commands and not managed_driver:
        raise errors.ExecutorError("INVALID_COVERAGE_INJECTION", "coverageCommands are not supported by this adapter")
    normalized_coverage_commands = {
        phase: _validate_command_spec(job, phase, command)
        for phase, command in coverage_commands.items()
    }
    libcstm_compile_arguments: list[str] = []
    diagnostic_log = raw.get("diagnosticLog")
    if driver == "MSBUILD":
        if not isinstance(diagnostic_log, str):
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "MSBUILD requires diagnosticLog")
        diagnostic_path = pathutil.canonical(diagnostic_log, False)
        if not pathutil.path_within(diagnostic_path, _job_directory(job)):
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "MSBUILD diagnosticLog must be under the current job")
        diagnostic_log = str(diagnostic_path)
        if raw.get("cleanBuild") is not True:
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "MSBUILD plan must declare cleanBuild=true")
    elif diagnostic_log is not None:
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "diagnosticLog is supported only by MSBUILD")
    expected_artifacts = raw.get("expectedArtifacts", [])
    if not isinstance(expected_artifacts, list) or not all(isinstance(value, str) and value for value in expected_artifacts):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "expectedArtifacts must be a string array")
    normalized_artifacts: list[str] = []
    for value in expected_artifacts:
        artifact = pathutil.canonical(value, False)
        if not (
            pathutil.path_within(artifact, job["projectRoot"])
            or pathutil.path_within(artifact, _job_directory(job))
        ):
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "expectedArtifacts contains an out-of-scope path", [value])
        normalized_artifacts.append(str(artifact))
    generator = raw.get("generator")
    configuration = raw.get("configuration")
    if generator is not None and not isinstance(generator, str):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "generator must be a string or null")
    if driver == "CMAKE":
        if generator == "":
            raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "generator must be null or a non-empty string")
    if configuration is not None and not isinstance(configuration, str):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "configuration must be a string or null")
    target_frameworks = raw.get("targetFrameworks", [])
    if not isinstance(target_frameworks, list) or not all(
        isinstance(value, str) and value and "\0" not in value for value in target_frameworks
    ):
        raise errors.ExecutorError("INVALID_ORIGINAL_BUILD_PLAN", "targetFrameworks must be a string array")
    normalized = {
        "schema": ORIGINAL_BUILD_PLAN_SCHEMA,
        "jobId": job["jobId"],
        "origin": origin,
        "evidenceSource": evidence_source,
        "environmentInitialization": environment_initialization,
        "driver": driver,
        "buildTool": str(build_tool),
        "adapterEntry": str(adapter_entry),
        "originalBuildDirectory": str(original_directory),
        "generator": generator,
        "configuration": configuration,
        "targetFrameworks": list(target_frameworks),
        "targets": list(targets),
        "requiredRoles": sorted(set(required_roles)),
        "targetTools": normalized_target_tools,
        "commands": normalized_commands,
        "coverageInjection": {
            "strategy": expected_strategy,
            "toolVariables": dict(tool_variables),
            "configureArguments": list(configure_arguments),
            "cleanTarget": clean_target,
            "forceRebuild": force_rebuild,
            "toolWrappers": dict(tool_wrappers),
            "coverageCommands": normalized_coverage_commands,
            "libcstmCompileArguments": libcstm_compile_arguments,
        },
        "diagnosticLog": diagnostic_log,
        "cleanBuild": raw.get("cleanBuild") is True,
        "expectedArtifacts": normalized_artifacts,
        "evidence": list(evidence),
    }
    return fileio.add_integrity(normalized)
