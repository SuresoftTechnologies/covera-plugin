from __future__ import annotations

"""Local Agent discovery, runtime-log, and wrapper-mapping helpers."""

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cover_build.common import open_api
from cover_build.common import pathutil
from cover_build.core import errors


DEFAULT_C_BUILD_LISTENER_PORT = 9090


def c_build_listener_port() -> int:
    raw = os.environ.get("CODESCROLL_TM_C_PORT", str(DEFAULT_C_BUILD_LISTENER_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise errors.ExecutorError("INVALID_AGENT_PORT", "CODESCROLL_TM_C_PORT must be an integer", [raw]) from exc
    if not 1 <= port <= 65535:
        raise errors.ExecutorError("INVALID_AGENT_PORT", "CODESCROLL_TM_C_PORT is outside the valid TCP port range", [raw])
    return port


def build_listener_port(capability: str) -> int:
    if capability != "CSHARP":
        return c_build_listener_port()
    raw = os.environ.get("CODESCROLL_TM_CS_PORT", "9092").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise errors.ExecutorError("INVALID_AGENT_PORT", "CODESCROLL_TM_CS_PORT must be an integer", [raw]) from exc
    if not 1 <= port <= 65535:
        raise errors.ExecutorError("INVALID_AGENT_PORT", "CODESCROLL_TM_CS_PORT is outside the valid TCP port range", [raw])
    return port


def windows_agent_type(build: int) -> str:
    return "Windows11" if build >= 22000 else "Windows10"


def current_agent_type() -> str:
    if os.name == "nt":
        return windows_agent_type(int(sys.getwindowsversion().build))
    system = re.sub(r"[^A-Za-z0-9_.-]+", "", sys.platform).lower()
    return system or "unknown"


def matching_agent(value: Any, hardware_id: str) -> dict[str, Any] | None:
    candidates = [value] if isinstance(value, dict) else open_api.response_list(value)
    matches = [
        item for item in candidates
        if isinstance(item, dict)
        and str(item.get("macAddress") or item.get("hardwareId") or "").casefold() == hardware_id.casefold()
    ]
    if len(matches) > 1:
        raise errors.ExecutorError(
            "AMBIGUOUS_AGENT_REGISTRATION",
            "More than one Agent has the local hardware identity",
            [str(item.get("id")) for item in matches],
        )
    return matches[0] if matches else None


def new_agent_alias(job: dict[str, Any], descriptor: dict[str, Any], hardware_id: str) -> str:
    host = re.sub(r"[^A-Za-z0-9_.-]+", "-", socket.gethostname()).strip("-") or "cover-agent"
    host = host[:32]
    environment = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", str(descriptor.get("agentEnv") or current_agent_type())
    ).strip("-") or "LOCAL"
    hardware_suffix = re.sub(r"[^A-Za-z0-9]+", "", hardware_id).lower()[-6:] or "unknown"
    job_suffix = re.sub(r"[^A-Za-z0-9]+", "", str(job.get("jobId") or "run")).lower()[-8:]
    return f"AI-Agent-Kit-{environment}-{host}-{hardware_suffix}-{job_suffix}"[:120]


def _monitoring_log_ready(log_text: str, expected_path: Path) -> bool:
    marker = "log monitor start watching:"
    watching_index = -1
    for index, line in enumerate(log_text.splitlines()):
        if marker not in line:
            continue
        observed = line.split(marker, 1)[1].strip()
        if observed and pathutil.same_path(observed, expected_path):
            watching_index = index
    if watching_index < 0:
        return False
    return any(
        "Sucess watch service init" in line or "Success watch service init" in line
        for line in log_text.splitlines()[watching_index:]
    )


def _read_log_tail(path: Path, limit: int = 512 * 1024) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        return stream.read().decode("utf-8", errors="replace")


def agent_runtime_log_state(agent_root: Path, monitor_path: Path) -> dict[str, Any]:
    log_root = agent_root / "logs"
    current = log_root / "testmonitor.log"
    candidates = [current]
    candidates.extend(sorted(log_root.glob("testmonitor.*.log"), key=lambda item: item.stat().st_mtime_ns, reverse=True)[:3])
    reporting_initialized = False
    monitoring_ready = False
    startup_log: Path | None = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        text = _read_log_tail(candidate)
        has_reporting = "AgentReportService Initial Success" in text
        has_monitor = _monitoring_log_ready(text, monitor_path)
        if has_reporting:
            reporting_initialized = True
            startup_log = startup_log or candidate
        if has_monitor:
            monitoring_ready = True
            startup_log = startup_log or candidate
    heartbeat_ready = False
    heartbeat_age_seconds: float | None = None
    if current.is_file():
        heartbeat_age_seconds = max(0.0, time.time() - current.stat().st_mtime)
        current_tail = _read_log_tail(current)
        heartbeat_ready = (
            heartbeat_age_seconds <= 15 * 60
            and "ThreadWatchDog" in current_tail
            and "AgentReportService" in current_tail
            and "LogMonitor" in current_tail
        )
    if heartbeat_ready:
        reporting_initialized = True
        monitoring_ready = True
    return {
        "reportingInitialized": reporting_initialized,
        "monitoringReady": monitoring_ready,
        "evidenceMode": "FRESH_WATCHDOG_HEARTBEAT" if heartbeat_ready else "STARTUP_LOG",
        "evidenceLog": str(current if heartbeat_ready else startup_log or current),
        "heartbeatAgeSeconds": heartbeat_age_seconds,
    }


def wrapper_kind(name: str) -> str | None:
    stem = name.casefold()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    if stem == "cl":
        return "C_COMPILER"
    if stem == "link":
        return "C_LINKER"
    if stem == "lib":
        return "ARCHIVER"
    if re.fullmatch(r".*gcc", stem):
        return "C_COMPILER"
    if re.fullmatch(r".*g\+\+", stem):
        return "CPP_COMPILER"
    if re.fullmatch(r".*ld", stem):
        return "C_LINKER"
    if re.fullmatch(r".*ar", stem):
        return "ARCHIVER"
    return None


def _mapped_real_tools(descriptor: dict[str, Any], names: set[str] | None = None) -> dict[str, Path]:
    info_root = Path(descriptor["toolchainProvisioning"]["toolchainInfoDirectory"])
    result: dict[str, Path] = {}
    for mapping in info_root.iterdir():
        selected = mapping.name.casefold() in names if names is not None else wrapper_kind(mapping.name) is not None
        if not selected or not mapping.is_file():
            continue
        value = mapping.read_text(encoding="utf-8-sig", errors="replace").strip().strip('"')
        if value:
            result[mapping.name] = pathutil.canonical(value)
    return result


def local_mappings_match(
    descriptor: dict[str, Any], expected: dict[str, Path], required_wrappers: set[str] | None
) -> bool:
    try:
        names = {name.casefold() for name in (required_wrappers or expected)}
        observed = _mapped_real_tools(descriptor, names)
    except (KeyError, FileNotFoundError, NotADirectoryError):
        return False
    observed_by_name = {name.casefold(): pathutil.canonical(path) for name, path in observed.items()}
    expected_by_name = {name.casefold(): pathutil.canonical(path) for name, path in expected.items()}
    required = {name.casefold() for name in required_wrappers} if required_wrappers else set(expected_by_name)
    return all(
        observed_by_name.get(name) is not None
        and expected_by_name.get(name) is not None
        and str(observed_by_name[name]).casefold() == str(expected_by_name[name]).casefold()
        for name in required
    )


def required_real_tools(
    descriptor: dict[str, Any],
    required_wrappers: set[str] | None,
    real_tools_override: dict[str, Path] | None,
) -> dict[str, Path]:
    available = real_tools_override if real_tools_override is not None else _mapped_real_tools(descriptor)
    requested = {name.casefold() for name in required_wrappers} if required_wrappers else {
        name.casefold() for name in available
    }
    result = {name: path for name, path in available.items() if name.casefold() in requested}
    missing = sorted(requested - {name.casefold() for name in result})
    if missing:
        raise errors.ExecutorError("AGENT_TOOLCHAIN_UNPROVISIONED", "Required local wrapper mappings are unavailable", missing)
    provisioning = descriptor.get("toolchainProvisioning") or {}
    root = descriptor.get("agentRoot")
    wrapper_dir = provisioning.get("wrapperDirectory") or (Path(root) / "wrapper" if root else None)
    info_dir = provisioning.get("toolchainInfoDirectory") or (Path(root) / "toolchaininfo" if root else None)
    wrapper_path = pathutil.canonical(wrapper_dir, False) if wrapper_dir else None
    info_path = pathutil.canonical(info_dir, False) if info_dir else None
    self_references = [
        {"wrapper": name, "realTool": str(path)} for name, path in result.items()
        if (wrapper_path is not None and pathutil.path_within(path, wrapper_path))
        or (info_path is not None and pathutil.path_within(path, info_path))
    ]
    if self_references:
        raise errors.ExecutorError(
            "WRAPPER_SELF_REFERENCE",
            "An Agent wrapper or toolchaininfo file cannot be registered as the original tool",
            self_references,
        )
    return result


def _architecture_from_text(value: str) -> str | None:
    searchable = value.casefold()
    if re.search(r"(?:aarch64|arm64|target=linux_aarch64|target=win_arm64)", searchable):
        return "arm64"
    if re.search(r"(?:armv[5-8]|arm32|target=linux_armv7)", searchable):
        return "arm32"
    if re.search(r"(?:x86_64|amd64|target=win64|target=linux_x86_64|64bit|mingw-w64)", searchable):
        return "x64"
    if re.search(r"(?:i[3-6]86|target=win32|target=linux_i686|32bit)", searchable):
        return "x86"
    return None


def _gcc_family(value: str) -> str | None:
    searchable = value.casefold()
    for family, patterns in {
        "mingw": ("mingw", "w64-mingw"), "cygwin": ("cygwin",),
        "linux": ("linux",), "stm32": ("stm32", "arm-none-eabi"),
    }.items():
        if any(pattern in searchable for pattern in patterns):
            return family
    return None


def toolchain_profile(wrapper: str, real_tool: Path, stm32cubeide: bool = False, *, include_evidence: bool = False) -> dict[str, Any]:
    is_cl = real_tool.name.casefold() == "cl.exe" or wrapper.casefold() == "cl.exe"
    completed = subprocess.run(
        [str(real_tool)] if is_cl else [str(real_tool), "--version"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", timeout=20, check=False,
    )
    version = re.search(r"(?:Version|version)\s+(\d+)\.(\d+)", completed.stdout)
    if not version:
        version = re.search(r"\b(\d+)\.(\d+)(?:\.\d+)?\b", completed.stdout)
    if not version:
        raise errors.ExecutorError("TOOLCHAIN_VERSION_UNRESOLVED", "Unable to parse compiler major/minor version", [str(real_tool)])
    major, minor = int(version.group(1)), int(version.group(2))
    target_kind = "x64"
    alias_name = f"{real_tool.stem.casefold()}-{major}.{minor}-x64"
    if not is_cl:
        machine = subprocess.run(
            [str(real_tool), "-dumpmachine"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
        )
        profile_text = "\n".join((completed.stdout, machine.stdout, str(real_tool)))
        target_kind = _architecture_from_text(profile_text) or "x64"
        family = _gcc_family(profile_text) or "gcc"
        if family == "stm32" and "arm-none-eabi" in profile_text.casefold():
            target_kind = "arm32"
        identity = real_tool.stem.casefold()
        if "clang" in profile_text.casefold() and "clang" not in identity:
            identity = f"clang-{identity}"
        alias_name = f"{family}-{identity}-{major}.{minor}-{target_kind}"
        if stm32cubeide and family == "stm32":
            alias_name = f"gcc {major} STM32"
            target_kind = "STM32"
    profile = {
        "aliasName": alias_name,
        "majorKind": "cl" if is_cl else "gcc",
        "minorKind": "general",
        "majorVersion": major,
        "minorVersion": minor,
        "targetKind": target_kind,
    }
    if include_evidence:
        profile["observedTool"] = str(real_tool)
        profile["versionOutput"] = completed.stdout
        profile["targetTriple"] = machine.stdout.strip() if not is_cl else None
    return profile
