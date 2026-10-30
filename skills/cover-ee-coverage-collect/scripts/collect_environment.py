"""Bounded, read-only discovery. Never attach to or stop a debugger."""
import json
import os
from pathlib import Path
import subprocess


def ide_processes():
    if os.name == "nt":
        command = "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^stm32cubeide(c)?\\.exe$' -or ($_.Name -match '^java(w)?\\.exe$' -and $_.CommandLine -match 'STM32CubeIDE') } | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=15, check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        value = json.loads(result.stdout) if result.stdout.strip() else []
        return value if isinstance(value, list) else [value]
    processes = []
    proc = Path("/proc")
    if not proc.is_dir():
        raise ValueError("Process discovery unavailable; specify IDE and workspace explicitly")
    for entry in proc.iterdir():
        if not entry.name.isdigit(): continue
        try:
            args = (entry / "cmdline").read_bytes().decode().strip("\0").split("\0")
            if not args or not (Path(args[0]).name.startswith("stm32cubeide") or
                (Path(args[0]).name in ("java", "javaw") and any("stm32cubeide" in a.lower() for a in args))): continue
            processes.append({"ProcessId": int(entry.name), "ExecutablePath": str((entry / "exe").resolve()), "argv": args})
        except (OSError, UnicodeError): continue
    return processes


def process_arguments(process):
    if "argv" in process: return process["argv"]
    # CommandLineToArgvW follows Windows quoting, unlike POSIX shlex.
    import ctypes
    from ctypes import wintypes
    count = ctypes.c_int()
    function = ctypes.windll.shell32.CommandLineToArgvW
    function.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    function.restype = ctypes.POINTER(wintypes.LPWSTR)
    pointer = function(process.get("CommandLine") or "", ctypes.byref(count))
    if not pointer: return []
    try: return [pointer[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))


def discover(project=None, workspace=None, processes=None):
    candidates = set()
    warnings = []
    if workspace: candidates.add(str(Path(workspace).resolve(strict=True)))
    elif project:
        project = Path(project).resolve(strict=True)
        for path in (project, *project.parents):
            if (path / ".metadata").is_dir(): candidates.add(str(path))
    try:
        processes = ide_processes() if processes is None else processes
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        processes = []
        warnings.append(f"PROCESS_DISCOVERY_FAILED: {type(error).__name__}; specify --workspace")
    visible = []
    for process in processes:
        args = process_arguments(process)
        item = {"pid": process["ProcessId"], "executable": process.get("ExecutablePath")}
        if "-data" in args and args.index("-data")+1 < len(args):
            value = Path(args[args.index("-data")+1])
            if value.is_absolute() and value.is_dir():
                item["workspace"] = str(value.resolve())
                if not workspace: candidates.add(item["workspace"])
        visible.append(item)
    return {"workspaces": sorted(candidates), "ideProcesses": visible, "warnings": warnings}


def require_closed(ide):
    ide = Path(ide).resolve(strict=True)
    # Fail closed if process inventory cannot be read. Do not terminate processes.
    for process in ide_processes():
        executable = process.get("ExecutablePath")
        if not executable:
            raise ValueError("IDE_PROCESS_UNRESOLVED: cannot prove selected IDE is closed")
        args = process_arguments(process)
        if Path(executable).resolve().is_relative_to(ide) or any(str(ide).lower() in a.lower() for a in args):
            raise ValueError("IDE_RUNNING: close the selected IDE after preserving test data")
