"""Visual Studio environment capture, overlay creation, and MSBuild coverage plans."""

from __future__ import annotations

import argparse
import locale
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.common import toolchains
from cover_build.core import errors


VS_ENV_SCHEMA = "cover-vs-environment/v1"


VS_OVERLAY = """<?xml version=\"1.0\" encoding=\"utf-8\"?>
<Project xmlns=\"http://schemas.microsoft.com/developer/msbuild/2003\">
  <PropertyGroup Condition=\"'$(CoverWrapperDirectory)' != ''\">
    <ExecutablePath>$(CoverWrapperDirectory);$(ExecutablePath)</ExecutablePath>
  </PropertyGroup>
</Project>
"""


def create_vs_overlay(args: argparse.Namespace) -> dict[str, Any]:
    output = pathutil.canonical(args.output, False)
    fileio.atomic_write_text(output, VS_OVERLAY)
    return envelopes.envelope(
        "CREATING_VS_OVERLAY",
        "SUCCEEDED",
        {
            "path": str(output),
            "sha256": pathutil.sha256_file(output),
            "importProperty": "ForceImportBeforeCppTargets",
            "preservationRule": "ExecutablePath := CoverWrapperDirectory + previous evaluated ExecutablePath",
        },
    )


def msbuild_windows_sdk_properties(environment: dict[str, str]) -> list[str]:
    """Pin a discovered Windows SDK without relying on optional SDK registry keys."""
    values = {key.upper(): value for key, value in environment.items()}
    sdk_root = values.get("WINDOWSSDKDIR", "").strip()
    version_with_separator = values.get("WINDOWSSDKVERSION", "").strip()
    version = version_with_separator.rstrip("\\/")
    if not sdk_root or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        return []
    root = Path(sdk_root)
    if not root.is_dir() or not (root / "Include" / version).is_dir():
        return []
    return [
        "/p:WindowsSdkDir_10=",
        f"/p:WindowsTargetPlatformVersion={version}",
        f"/p:WindowsSdkDir={root}{os.sep}",
        f"/p:WindowsSDKVersion={version}{os.sep}",
    ]


def capture_vs_environment(args: argparse.Namespace) -> dict[str, Any]:
    if os.name != "nt":
        raise errors.ExecutorError("UNSUPPORTED_HOST", "Visual Studio environment capture requires Windows")
    vsdevcmd = pathutil.canonical(args.vsdevcmd)
    if vsdevcmd.name.lower() != "vsdevcmd.bat":
        raise errors.ExecutorError("INVALID_VSDEVCMD", f"Expected VsDevCmd.bat: {vsdevcmd}")
    allowed_arches = {"x86", "x64", "arm", "arm64"}
    if args.arch not in allowed_arches or args.host_arch not in {"x86", "x64", "arm64"}:
        raise errors.ExecutorError("INVALID_VS_ARCHITECTURE", "Unsupported Visual Studio architecture")
    safe_names = {
        "ALLUSERSPROFILE", "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH",
        "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "OS", "PATH", "PATHEXT",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMW6432", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP",
        "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR",
    }
    included_names: list[str] = []
    for name in getattr(args, "include_env", []):
        normalized = name.strip().upper()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", normalized):
            raise errors.ExecutorError("INVALID_ENVIRONMENT_NAME", f"Invalid environment variable name: {name}")
        if re.search(r"TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API_?KEY|PRIVATE_?KEY", normalized):
            raise errors.ExecutorError("SENSITIVE_ENVIRONMENT_REJECTED", f"Refusing to capture sensitive environment variable: {name}")
        safe_names.add(normalized)
        included_names.append(normalized)
    safe_env = {key: value for key, value in os.environ.items() if key.upper() in safe_names}
    batch_text = (
        "@echo off\r\n"
        f'call "{vsdevcmd}" -no_logo -arch={args.arch} -host_arch={args.host_arch}\r\n'
        "if errorlevel 1 exit /b %errorlevel%\r\n"
        "set\r\n"
    )
    fd, batch_name = tempfile.mkstemp(prefix="cover-vsenv-", suffix=".cmd")
    try:
        with os.fdopen(fd, "w", encoding=locale.getpreferredencoding(False), newline="") as stream:
            stream.write(batch_text)
        process = subprocess.run(
            [os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/d", "/c", batch_name],
            env=safe_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=args.timeout,
            shell=False,
            check=False,
        )
    finally:
        if os.path.exists(batch_name):
            os.unlink(batch_name)
    if process.returncode != 0:
        raise errors.ExecutorError("VSDEVCMD_FAILED", f"VsDevCmd exited with {process.returncode}", [process.stdout])
    captured: dict[str, str] = {}
    for line in process.stdout.splitlines():
        if "=" not in line or line.startswith("="):
            continue
        key, value = line.split("=", 1)
        captured[key] = value
    required = ["PATH", "VSCMD_VER", "VSINSTALLDIR", "VCINSTALLDIR"]
    missing = [key for key in required if not any(existing.upper() == key for existing in captured)]
    if missing:
        raise errors.ExecutorError("VSDEVCMD_INCOMPLETE", "Captured environment is missing: " + ", ".join(missing))
    descriptor = fileio.add_integrity(
        {
            "schema": VS_ENV_SCHEMA,
            "source": str(vsdevcmd),
            "arch": args.arch,
            "hostArch": args.host_arch,
            "includedCallerEnvironment": sorted(set(included_names)),
            "environment": captured,
        }
    )
    if args.output:
        fileio.atomic_write_json(pathutil.canonical(args.output, False), descriptor)
    return envelopes.envelope("CAPTURING_VS_ENVIRONMENT", "SUCCEEDED", descriptor)


def resolve_vc_targets_path(msbuild: Path, environment: dict[str, str], explicit: str | None) -> Path:
    if explicit:
        candidate = pathutil.canonical(explicit)
    else:
        version = next((value for key, value in environment.items() if key.upper() == "VISUALSTUDIOVERSION"), "")
        match = re.fullmatch(r"(\d+)(?:\.\d+)?", version.strip())
        if not match:
            raise errors.ExecutorError("VS_VERSION_MISSING", "Captured environment has no valid VisualStudioVersion")
        msbuild_root = next((parent for parent in msbuild.parents if parent.name.lower() == "msbuild"), None)
        if msbuild_root is None:
            raise errors.ExecutorError("MSBUILD_LAYOUT_UNSUPPORTED", f"Cannot derive the MSBuild root from {msbuild}")
        candidate = msbuild_root / "Microsoft" / "VC" / f"v{match.group(1)}0"
        candidate = pathutil.canonical(candidate)
    required = [candidate / "Microsoft.Cpp.Default.props", candidate / "Microsoft.Cpp.props", candidate / "Microsoft.Cpp.targets"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise errors.ExecutorError("VC_TARGETS_INCOMPLETE", "Selected VCTargetsPath is incomplete", missing)
    return candidate


def plan_vs(args: argparse.Namespace) -> dict[str, Any]:
    msbuild = pathutil.canonical(args.msbuild)
    project = pathutil.canonical(args.project)
    project_root = pathutil.canonical(args.project_root or project.parent)
    wrapper = pathutil.canonical(args.wrapper_dir)
    if wrapper.name.casefold() != "wrapper":
        raise errors.ExecutorError("INVALID_AGENT_WRAPPER_DIRECTORY", "COVER wrapper directory must be <agent-root>/wrapper")
    agent_root = wrapper.parent
    overlay = pathutil.canonical(args.overlay)
    environment_descriptor = fileio.load_json(args.environment)
    if environment_descriptor.get("schema") != VS_ENV_SCHEMA:
        raise errors.ExecutorError("INVALID_VS_ENVIRONMENT", f"Expected {VS_ENV_SCHEMA}")
    fileio.verify_integrity(environment_descriptor)
    build_environment: dict[str, str] = environment_descriptor["environment"]
    # Never trust the caller's inherited installation root. Wrapper binaries
    # load config, tmp, include, and runtime files through this variable.
    build_environment["CODESCROLL_TM_INSTALL_PATH"] = str(agent_root)
    vc_targets_path = resolve_vc_targets_path(msbuild, build_environment, args.vc_targets_path)
    # Microsoft.Cpp.ToolsetLocation.props concatenates this property with
    # relative paths and therefore requires a trailing directory separator.
    vc_targets_property = f"{vc_targets_path}{os.sep}"
    windows_sdk_properties = msbuild_windows_sdk_properties(build_environment)
    wrapper_verification_names = [str(name).casefold() for name in (getattr(args, "required_wrapper", None) or [])]
    supported_wrapper_names = {"cl.exe", "link.exe", "lib.exe"}
    if not wrapper_verification_names or any(name not in supported_wrapper_names for name in wrapper_verification_names):
        raise errors.ExecutorError(
            "INVALID_MSBUILD_WRAPPER_EXPECTATION",
            "MSBuild plan requires one or more cl.exe, link.exe, or lib.exe wrapper expectations",
            wrapper_verification_names,
        )
    real_tools = {
        name.casefold(): path
        for name, path in (toolchains.parse_tool_argument(value) for value in (getattr(args, "real_tool", None) or []))
    }
    if set(real_tools) != set(wrapper_verification_names):
        raise errors.ExecutorError(
            "INVALID_MSBUILD_WRAPPER_EXPECTATION",
            "MSBuild real-tool evidence must match the required wrapper names",
            sorted(set(wrapper_verification_names) ^ set(real_tools)),
        )
    if project.suffix.lower() not in {".sln", ".vcxproj"}:
        raise errors.ExecutorError("UNSUPPORTED_BUILD_SYSTEM", "MSBuild plan requires .sln or .vcxproj")
    if not str(project).lower().startswith(str(project_root).lower()):
        raise errors.ExecutorError("PROJECT_OUTSIDE_ROOT", f"{project} is outside {project_root}")
    common = [
        str(project),
        "/nologo",
        f"/p:Configuration={args.configuration}",
        f"/p:Platform={args.platform}",
        f"/p:VCTargetsPath={vc_targets_property}",
        f"/p:ForceImportBeforeCppTargets={overlay}",
        f"/p:CoverWrapperDirectory={wrapper}",
    ] + windows_sdk_properties
    plan = fileio.add_integrity(
        {
            "schema": processes.PLAN_SCHEMA,
            "jobId": envelopes.CURRENT_JOB_ID,
            "adapter": "MSBUILD_VCXPROJ",
            "projectRoot": str(project_root),
            "configuration": args.configuration,
            "platform": args.platform,
            "wrapperDirectory": str(wrapper),
            "wrapperVerificationNames": sorted(set(wrapper_verification_names)),
            "realTools": {name: str(path) for name, path in real_tools.items()},
            "agentRoot": str(agent_root),
            "overlay": str(overlay),
            "vcTargetsPath": str(vc_targets_path),
            "windowsSdkProperties": windows_sdk_properties,
            "commands": {
                "rebuild": processes.command_spec(
                    msbuild,
                    common + [f"/m:{getattr(args, 'parallel', processes.default_parallelism())}", "/t:Rebuild", "/verbosity:diagnostic"],
                    project.parent,
                    args.timeout,
                ),
            },
            "expectedArtifacts": [str(pathutil.canonical(path, False)) for path in args.artifact],
        }
    )
    for phase in ("rebuild",):
        plan["commands"][phase]["environmentMode"] = "replace"
        plan["commands"][phase]["environment"] = build_environment
    plan = fileio.add_integrity(plan)
    if args.output:
        fileio.atomic_write_json(pathutil.canonical(args.output, False), plan)
    return envelopes.envelope("PLANNING_BUILD", "SUCCEEDED", plan)
