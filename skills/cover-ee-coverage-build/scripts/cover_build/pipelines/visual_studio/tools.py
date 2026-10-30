from __future__ import annotations

import json
import locale
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


from cover_build.core.errors import WorkflowWaiting
from cover_build.common import native_logs
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors
from cover_build.pipelines.visual_studio import planning as vs_planning


def _find_vswhere() -> Path:
    locations = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
        Path(os.environ.get("ProgramFiles", r"C:\\Program Files")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
    ]
    vswhere = next((item for item in locations if item.is_file()), None)
    if not vswhere:
        raise errors.ExecutorError("VISUAL_STUDIO_NOT_FOUND", "vswhere.exe was not found")
    return vswhere


def _tools_under_installation(root: Path) -> tuple[Path, Path] | None:
    candidates = [root / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe", root / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"]
    msbuild = next((item for item in candidates if item.is_file()), None)
    vsdevcmd = root / "Common7" / "Tools" / "VsDevCmd.bat"
    if not msbuild or not vsdevcmd.is_file():
        return None
    return msbuild, vsdevcmd


def _installation_root_for(path: Path) -> Path | None:
    for candidate in (path.parent, *path.parents):
        if (candidate / "Common7" / "Tools" / "VsDevCmd.bat").is_file():
            return candidate
    return None


def discover_tool_candidates() -> list[dict[str, str]]:
    vswhere = _find_vswhere()
    process = subprocess.run(
        [
            str(vswhere),
            "-all",
            "-products",
            "*",
            "-requires",
            "Microsoft.Component.MSBuild",
            "-format",
            "json",
            "-utf8",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if process.returncode != 0:
        raise errors.ExecutorError(
            "VISUAL_STUDIO_NOT_FOUND",
            "vswhere could not enumerate Visual Studio installations",
            [process.stdout],
        )
    try:
        installations = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise errors.ExecutorError(
            "VISUAL_STUDIO_DISCOVERY_INVALID",
            "vswhere did not return valid JSON",
            [str(exc)],
        ) from exc
    if not isinstance(installations, list):
        raise errors.ExecutorError("VISUAL_STUDIO_DISCOVERY_INVALID", "vswhere JSON must be an array")
    result: list[dict[str, str]] = []
    for installation in installations:
        if not isinstance(installation, dict) or not isinstance(installation.get("installationPath"), str):
            continue
        root = Path(installation["installationPath"])
        tools = _tools_under_installation(root)
        if tools is None:
            continue
        msbuild, vsdevcmd = tools
        result.append(
            {
                "installationPath": str(root),
                "installationVersion": str(installation.get("installationVersion") or ""),
                "displayName": str(installation.get("displayName") or ""),
                "msbuildPath": str(msbuild),
                "vsDevCmdPath": str(vsdevcmd),
            }
        )
    if not result:
        raise errors.ExecutorError(
            "VISUAL_STUDIO_LAYOUT_UNSUPPORTED",
            "No Visual Studio installation contains both MSBuild.exe and VsDevCmd.bat",
        )
    return result


def find_tools(
    explicit_msbuild: str | Path | None = None,
    explicit_vsdevcmd: str | Path | None = None,
    preferred_platform_toolsets: set[str] | None = None,
) -> tuple[Path, Path]:
    """Resolve one coherent MSBuild/VsDevCmd pair without assuming latest is correct."""
    if explicit_msbuild is not None:
        msbuild = pathutil.canonical(explicit_msbuild)
        if msbuild.name.casefold() != "msbuild.exe":
            raise errors.ExecutorError(
                "INVALID_MSBUILD_SELECTION",
                "The selected Visual Studio buildTool must be MSBuild.exe",
                [str(msbuild)],
            )
        if explicit_vsdevcmd is not None:
            vsdevcmd = pathutil.canonical(explicit_vsdevcmd)
        else:
            root = _installation_root_for(msbuild)
            if root is None:
                raise WorkflowWaiting(
                    "VSDEVCMD_SELECTION_REQUIRED",
                    "The selected MSBuild is not under a Visual Studio installation whose VsDevCmd.bat can be derived",
                    {"vsDevCmdPath": []},
                    decision_owner="MODEL",
                    purpose="BUILD_ENVIRONMENT",
                    evidence=[str(msbuild)],
                )
            vsdevcmd = pathutil.canonical(root / "Common7" / "Tools" / "VsDevCmd.bat")
        if vsdevcmd.name.casefold() != "vsdevcmd.bat":
            raise errors.ExecutorError(
                "INVALID_VSDEVCMD_SELECTION",
                "The selected Visual Studio environment entry must be VsDevCmd.bat",
                [str(vsdevcmd)],
            )
        msbuild_root = _installation_root_for(msbuild)
        vsdevcmd_root = _installation_root_for(vsdevcmd)
        if msbuild_root is not None and vsdevcmd_root is not None and not pathutil.same_path(msbuild_root, vsdevcmd_root):
            raise errors.ExecutorError(
                "VISUAL_STUDIO_INSTALLATION_MISMATCH",
                "MSBuild.exe and VsDevCmd.bat belong to different Visual Studio installations",
                [str(msbuild_root), str(vsdevcmd_root)],
            )
        return msbuild, vsdevcmd

    candidates = discover_tool_candidates()
    if explicit_vsdevcmd is not None:
        vsdevcmd = pathutil.canonical(explicit_vsdevcmd)
        matching = [item for item in candidates if pathutil.same_path(item["vsDevCmdPath"], vsdevcmd)]
        if len(matching) != 1:
            raise errors.ExecutorError(
                "INVALID_VSDEVCMD_SELECTION",
                "The selected VsDevCmd.bat does not match one discovered Visual Studio installation",
                [str(vsdevcmd)],
            )
        return pathutil.canonical(matching[0]["msbuildPath"]), vsdevcmd
    if len(candidates) == 1:
        return pathutil.canonical(candidates[0]["msbuildPath"]), pathutil.canonical(candidates[0]["vsDevCmdPath"])
    toolset_to_installation_major = {"v143": "17", "v142": "16", "v141": "15", "v140": "14"}
    preferred_majors = {
        toolset_to_installation_major[value.casefold()]
        for value in (preferred_platform_toolsets or set())
        if value.casefold() in toolset_to_installation_major
    }
    if len(preferred_majors) == 1:
        preferred_major = next(iter(preferred_majors))
        matching = [
            item
            for item in candidates
            if item["installationVersion"].split(".", 1)[0] == preferred_major
        ]
        if len(matching) == 1:
            return pathutil.canonical(matching[0]["msbuildPath"]), pathutil.canonical(matching[0]["vsDevCmdPath"])
    raise WorkflowWaiting(
        "VISUAL_STUDIO_INSTALLATION_SELECTION_REQUIRED",
        "More than one Visual Studio MSBuild installation is available; select the customer build/CI MSBuild or the installation that supports the evaluated project toolset",
        {"msbuildPath": [item["msbuildPath"] for item in candidates]},
        decision_owner="MODEL",
        purpose="BUILD_ENVIRONMENT",
        evidence=[f"{item['displayName']} {item['installationVersion']} | {item['msbuildPath']}" for item in candidates],
    )


def project_platform_toolsets(projects: list[str | Path]) -> set[str]:
    result: set[str] = set()
    for project in projects:
        path = Path(project)
        if path.suffix.casefold() != ".vcxproj" or not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "PlatformToolset" and element.text and element.text.strip():
                result.add(element.text.strip())
    return result


def resolve_path_tool(environment: dict[str, str], filename: str) -> Path:
    path_value = next((value for key, value in environment.items() if key.upper() == "PATH"), "")
    for directory in pathutil.split_path_list(path_value):
        candidate = Path(directory) / filename
        if candidate.is_file():
            return pathutil.canonical(candidate)
    raise errors.ExecutorError("COMPILER_NOT_FOUND", f"{filename} was not found in captured Visual Studio PATH")


MSVC_TOOL_ROLES = {
    "cl.exe": ("compiler", "cl.exe"),
    "clang-cl.exe": ("compiler", "cl.exe"),
    "link.exe": ("linker", "link.exe"),
    "lld-link.exe": ("linker", "link.exe"),
    "lib.exe": ("archiver", "lib.exe"),
}


def _diagnostic_tool_paths(line: str) -> list[str]:
    quoted = re.findall(
        r'["\']([A-Za-z]:[\\/][^"\'\r\n]+?[\\/](?:cl|clang-cl|link|lld-link|lib)\.exe)["\']',
        line,
        flags=re.IGNORECASE,
    )
    unquoted = re.findall(
        r'([A-Za-z]:[\\/].*?[\\/](?:cl|clang-cl|link|lld-link|lib)\.exe)(?=\s+(?:/|-))',
        line,
        flags=re.IGNORECASE,
    )
    result: list[str] = []
    for value in [*quoted, *unquoted]:
        cleaned = value.strip().strip('"').strip("'")
        if cleaned not in result:
            result.append(cleaned)
    return result


def extract_toolchain_from_diagnostic(
    log_path: Path,
    artifacts: Path | list[Path],
    *,
    working_directories: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Extract tool paths only from command-shaped MSBuild diagnostic lines."""
    raw = log_path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    response_search_directories = [Path(value) for value in working_directories or []]
    candidates: dict[str, list[dict[str, Any]]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        analysis = native_logs.analyze_native_command_text(line)
        response_expansion = native_logs.expand_response_file_tokens(
            analysis["tokens"],
            response_search_directories[0] if response_search_directories else None,
            search_directories=response_search_directories[1:],
        )
        expanded_tokens = response_expansion["tokens"]
        has_compile_flag = any(token.casefold() in {"-c", "/c"} for token in expanded_tokens)
        has_output_flag = any(token.casefold().startswith("/out:") for token in expanded_tokens)
        for raw_path in _diagnostic_tool_paths(line):
            path = Path(raw_path)
            definition = MSVC_TOOL_ROLES.get(path.name.casefold())
            if definition is None:
                continue
            role, wrapper = definition
            if role == "compiler" and not has_compile_flag:
                continue
            if role in {"linker", "archiver"} and not has_output_flag:
                continue
            try:
                canonical = pathutil.canonical(path)
            except errors.ExecutorError:
                continue
            item = {
                "role": role,
                "wrapper": wrapper,
                "path": str(canonical),
                "provenance": "MSBUILD_DIAGNOSTIC_COMMAND",
                "line": line[:1000],
            }
            if response_expansion["responseFiles"]:
                item["responseFiles"] = response_expansion["responseFiles"]
            if response_expansion["unresolvedResponseFiles"]:
                item["unresolvedResponseFiles"] = response_expansion["unresolvedResponseFiles"]
            role_candidates = candidates.setdefault(wrapper, [])
            if not any(pathutil.same_path(current["path"], canonical) for current in role_candidates):
                role_candidates.append(item)

    artifact_list = [artifacts] if isinstance(artifacts, Path) else list(artifacts)
    required = {"cl.exe"}
    required.update(
        "lib.exe" if artifact.suffix.casefold() == ".lib" else "link.exe"
        for artifact in artifact_list
    )
    ambiguous = {name: [item["path"] for item in items] for name, items in candidates.items() if len(items) > 1}
    if ambiguous:
        raise WorkflowWaiting(
            "ORIGINAL_TOOLCHAIN_AMBIGUOUS",
            "The original MSBuild used more than one executable for the same tool role; do not collapse project toolsets into one Agent wrapper mapping",
            {
                "originalToolPath": ambiguous,
            },
            decision_owner="MODEL",
            purpose="BUILD_ENVIRONMENT",
            evidence=[str(log_path)],
        )
    missing = sorted(required - set(candidates))
    if missing:
        raise WorkflowWaiting(
            "ORIGINAL_TOOLCHAIN_UNRESOLVED",
            "The original MSBuild succeeded but its required tool paths were not found in the diagnostic command lines",
            {"originalToolPath": missing},
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
            evidence=[str(log_path)],
        )
    tools = {name: items[0]["path"] for name, items in candidates.items()}
    evidence = {name: items[0] for name, items in candidates.items()}
    return {
        "tools": tools,
        "evidence": evidence,
        "unusedRoles": [
            role
            for role, wrapper in (("compiler", "cl.exe"), ("linker", "link.exe"), ("archiver", "lib.exe"))
            if wrapper not in tools
        ],
    }


def apply_tool_overrides(observed: dict[str, Any], overrides: list[tuple[str, Path]]) -> dict[str, Any]:
    supported = {"cl.exe", "link.exe", "lib.exe"}
    result = {
        "tools": dict(observed.get("tools", {})),
        "evidence": dict(observed.get("evidence", {})),
        "unusedRoles": list(observed.get("unusedRoles", [])),
    }
    explicit: set[str] = set()
    for raw_name, raw_path in overrides:
        name = raw_name.casefold()
        if name not in supported:
            raise errors.ExecutorError(
                "INVALID_VISUAL_STUDIO_TOOL_OVERRIDE",
                f"Unsupported Visual Studio tool override: {raw_name}",
                sorted(supported),
            )
        if name in explicit:
            raise errors.ExecutorError(
                "DUPLICATE_VISUAL_STUDIO_TOOL_OVERRIDE",
                f"Visual Studio tool override was provided more than once: {raw_name}",
            )
        explicit.add(name)
        path = pathutil.canonical(raw_path)
        actual = result["tools"].get(name)
        if actual and not pathutil.same_path(actual, path):
            raise errors.ExecutorError(
                "VISUAL_STUDIO_TOOL_OVERRIDE_CONFLICT",
                "A tool override conflicts with the executable observed in the original MSBuild diagnostic log",
                [f"{name}: observed={actual}", f"{name}: override={path}"],
            )
        if not actual:
            result["tools"][name] = str(path)
            result["evidence"][name] = {
                "wrapper": name,
                "path": str(path),
                "provenance": "EXPLICIT_FALLBACK_NOT_OBSERVED",
            }
    return result


def reject_cover_wrappers(tools: dict[str, str]) -> None:
    install = os.environ.get("CODESCROLL_TM_INSTALL_PATH")
    install_path = pathutil.canonical(install, False) if install else None
    offenders = []
    for value in tools.values():
        path = pathutil.canonical(value)
        if path.parent.name.casefold() == "wrapper" or (install_path and pathutil.path_within(path, install_path)):
            offenders.append(str(path))
    if offenders:
        raise WorkflowWaiting(
            "ORIGINAL_BUILD_USED_COVER_WRAPPER",
            "The supposed original MSBuild invoked COVER wrapper executables",
            {
                "originalBuildPlan": [],
                "originalBuildCommandSource": ["MODEL_INFERRED"],
            },
            decision_owner="MODEL",
            purpose="ORIGINAL_BUILD_COMMAND",
            evidence=offenders,
        )


def read_target_path(
    msbuild: Path,
    project: Path,
    configuration: str,
    platform: str,
    environment: dict[str, str],
    log_path: Path,
    solution_dir: Path | None = None,
) -> Path:
    arguments = [
        str(project),
        "/nologo",
        f"/p:Configuration={configuration}",
        f"/p:Platform={platform}",
        *([f"/p:SolutionDir={solution_dir}{os.sep}"] if solution_dir else []),
        *vs_planning.msbuild_windows_sdk_properties(environment),
        "/getProperty:TargetPath",
    ]
    spec = processes.command_spec(msbuild, arguments, project.parent, 60)
    spec["environmentMode"] = "replace"
    spec["environment"] = environment
    result = processes.run_command(spec, log_path)
    if result["exitCode"] != 0:
        raise errors.ExecutorError("TARGET_PATH_QUERY_FAILED", "MSBuild could not evaluate TargetPath", [result["log"]])
    match = re.search(
        r"(?:TargetPath\s*=\s*)?([A-Za-z]:[^\r\n\"]+\.(?:exe|dll|lib))",
        processes.read_output_tail(result["log"]),
        re.IGNORECASE,
    )
    if not match:
        raise WorkflowWaiting(
            "TARGET_OUTPUT_AMBIGUOUS",
            "MSBuild did not emit one parseable TargetPath",
            {"expectedArtifacts": []},
            decision_owner="MODEL",
            purpose="BUILD_OUTPUT",
            evidence=[result["log"]],
        )
    return pathutil.canonical(match.group(1).strip(), False)


def is_utility_configuration(project: Path, configuration: str, platform: str) -> bool:
    """Identify an MSBuild Utility project without requiring a newer /getProperty implementation."""
    try:
        root = ET.parse(project).getroot()
    except (OSError, ET.ParseError):
        return False
    selected = f"{configuration}|{platform}".casefold()
    matched_types: list[str] = []
    for group in root.iter():
        if group.tag.rsplit("}", 1)[-1] != "PropertyGroup":
            continue
        condition = group.attrib.get("Condition", "")
        if condition:
            values = re.findall(r"==\s*['\"]([^'\"]+\|[^'\"]+)['\"]", condition)
            if values and selected not in {value.casefold() for value in values}:
                continue
        for node in group:
            if node.tag.rsplit("}", 1)[-1] == "ConfigurationType" and node.text:
                matched_types.append(node.text.strip())
    return bool(matched_types) and all(value.casefold() == "utility" for value in matched_types)
