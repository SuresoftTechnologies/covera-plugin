"""Native build log parsing: shell segments, response files, and tool roles."""

from __future__ import annotations

import ctypes
import hashlib
import locale
import os
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import pathutil
from cover_build.common import processes
from cover_build.core import errors


def split_shell_segments(line: str) -> list[str]:
    """Split one native command line at unquoted shell control operators."""
    segments: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(line):
        character = line[index]
        if character in {'"', "'"}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
            index += 1
            continue
        if quote is None:
            pair = line[index : index + 2]
            width = 2 if pair in {"&&", "||", "|&"} else 1 if character in {";", "|", "&"} else 0
            if width:
                segment = line[start:index].strip()
                if segment:
                    segments.append(segment)
                start = index + width
                index += width
                continue
        index += 1
    tail = line[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def _decode_response_file(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _split_response_file_text(text: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(text, posix=True)
    argc = ctypes.c_int()
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    # CommandLineToArgvW applies special rules to argv[0]. Prefix a synthetic
    # executable so the first response-file option is parsed like a normal
    # compiler argument (for example @"nested file.rsp").
    argv = shell32.CommandLineToArgvW(f"response-file {text}", ctypes.byref(argc))
    if not argv:
        raise ValueError("CommandLineToArgvW could not tokenize the response file")
    try:
        return [argv[index] for index in range(1, argc.value)]
    finally:
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def expand_response_file_tokens(
    tokens: list[str],
    command_directory: str | Path | None,
    *,
    search_directories: Iterable[str | Path] | None = None,
    max_depth: int = 4,
    max_files: int = 64,
    max_file_bytes: int = 4 * 1024 * 1024,
    max_total_bytes: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    """Expand local compiler response files without executing their contents."""
    roots: list[Path] = []

    def add_root(value: str | Path | None) -> None:
        if value is None:
            return
        root = pathutil.canonical(value, False)
        if not any(pathutil.same_path(root, current) for current in roots):
            roots.append(root)

    add_root(command_directory)
    for directory in search_directories or []:
        add_root(directory)

    response_files: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    total_bytes = 0

    def resolve(token: str) -> tuple[Path | None, list[Path]]:
        raw_path = token[1:].strip().strip('"').strip("'")
        if not raw_path:
            return None, []
        candidate = Path(raw_path)
        candidates = [candidate] if candidate.is_absolute() else [root / candidate for root in roots]
        existing: list[Path] = []
        for item in candidates:
            if not item.is_file():
                continue
            resolved = pathutil.canonical(item)
            if not any(pathutil.same_path(resolved, current) for current in existing):
                existing.append(resolved)
        return (existing[0] if len(existing) == 1 else None), existing

    def expand(values: list[str], depth: int, active: set[str]) -> list[str]:
        nonlocal total_bytes
        expanded: list[str] = []
        for raw_token in values:
            token = raw_token.strip().strip('"').strip("'")
            if not token.startswith("@") or len(token) == 1:
                expanded.append(token)
                continue
            path, candidates = resolve(token)
            if path is None:
                unresolved.append(
                    {
                        "token": token,
                        "reason": "AMBIGUOUS" if len(candidates) > 1 else "NOT_FOUND",
                        "candidates": [str(item) for item in candidates],
                    }
                )
                expanded.append(token)
                continue
            identity = os.path.normcase(str(path))
            if identity in active:
                unresolved.append({"token": token, "reason": "CYCLE", "candidates": [str(path)]})
                expanded.append(token)
                continue
            if depth >= max_depth:
                unresolved.append({"token": token, "reason": "MAX_DEPTH", "candidates": [str(path)]})
                expanded.append(token)
                continue
            size = path.stat().st_size
            if size > max_file_bytes:
                unresolved.append({"token": token, "reason": "FILE_TOO_LARGE", "candidates": [str(path)]})
                expanded.append(token)
                continue
            if len(response_files) >= max_files or total_bytes + size > max_total_bytes:
                unresolved.append({"token": token, "reason": "TOTAL_LIMIT", "candidates": [str(path)]})
                expanded.append(token)
                continue
            raw = path.read_bytes()
            total_bytes += len(raw)
            response_files.append(
                {
                    "path": str(path),
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "depth": depth + 1,
                }
            )
            try:
                nested = _split_response_file_text(_decode_response_file(raw))
            except ValueError:
                unresolved.append({"token": token, "reason": "TOKENIZE_FAILED", "candidates": [str(path)]})
                expanded.append(token)
                continue
            normalized = [value.strip().strip('"').strip("'") for value in nested if value.strip()]
            expanded.extend(expand(normalized, depth + 1, {*active, identity}))
        return expanded

    return {
        "tokens": expand(tokens, 0, set()),
        "responseFiles": response_files,
        "unresolvedResponseFiles": unresolved,
    }


def analyze_native_command_text(text: str) -> dict[str, list[str]]:
    """Return executable positions plus tokens for conservative bypass checks."""
    executables: list[str] = []
    command_tokens: list[str] = []

    def inspect(command: str, depth: int = 0) -> None:
        if depth > 4:
            return
        for segment in split_shell_segments(command):
            # shlex(posix=False) splits @"path with spaces.rsp" at the
            # embedded space because the quote does not start the token.
            # Move the @ inside the quotes for tokenization, then strip the
            # quotes below so the observed token remains @path with spaces.rsp.
            tokenizable = re.sub(
                r"@([\"'])(.*?)\1",
                lambda match: f"{match.group(1)}@{match.group(2)}{match.group(1)}",
                segment,
            )
            try:
                tokens = shlex.split(tokenizable, posix=False)
            except ValueError:
                continue
            tokens = [token.strip().strip('"').strip("'") for token in tokens if token.strip()]
            command_tokens.extend(tokens)
            while tokens and tokens[0] in {"@", "-"}:
                tokens.pop(0)
            if tokens:
                tokens[0] = tokens[0].lstrip("@-")
            while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
                tokens.pop(0)
            if not tokens:
                continue
            first_name = Path(tokens[0]).name.casefold()
            if first_name in {"env", "env.exe"}:
                tokens.pop(0)
                while tokens and (tokens[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0])):
                    tokens.pop(0)
                if not tokens:
                    continue
                first_name = Path(tokens[0]).name.casefold()
            executables.append(tokens[0])
            if first_name in {"ccache", "ccache.exe", "sccache", "sccache.exe"} and len(tokens) > 1:
                executables.append(tokens[1])
            if first_name in {"cmd", "cmd.exe"}:
                switch = next((index for index, token in enumerate(tokens[1:], start=1) if token.casefold() in {"/c", "/k"}), None)
                if switch is not None and switch + 1 < len(tokens):
                    inspect(" ".join(tokens[switch + 1 :]), depth + 1)
            elif first_name in {"sh", "sh.exe", "bash", "bash.exe"}:
                switch = next((index for index, token in enumerate(tokens[1:], start=1) if token == "-c"), None)
                if switch is not None and switch + 1 < len(tokens):
                    inspect(" ".join(tokens[switch + 1 :]), depth + 1)

    for raw_line in text.splitlines():
        if raw_line.strip():
            inspect(raw_line.strip())
    return {"executables": executables, "tokens": command_tokens}


def native_command_executables(text: str) -> list[str]:
    """Return executable-position tokens, not wrapper-looking argument text."""
    return analyze_native_command_text(text)["executables"]


def verify_gcc_preflight_text(
    plan: dict[str, Any],
    text: str | Path,
    *,
    conservative_native_tokens: bool = True,
) -> dict[str, Any]:
    wrappers = plan.get("wrappers")
    roles = plan.get("wrapperVerificationRoles", plan.get("preflightRequiredRoles"))
    if not isinstance(wrappers, dict) or not isinstance(roles, list) or not roles:
        raise errors.ExecutorError("INVALID_GCC_PREFLIGHT", "GCC plan has no wrapper expectations")
    real_tools = plan.get("realTools", {})
    if not isinstance(real_tools, dict):
        raise errors.ExecutorError("INVALID_GCC_PREFLIGHT", "GCC plan realTools must be an object")
    native_bypass_roles = plan.get("nativeToolBypassRoles", [])
    if not isinstance(native_bypass_roles, list) or not all(isinstance(role, str) for role in native_bypass_roles):
        raise errors.ExecutorError("INVALID_GCC_PREFLIGHT", "GCC plan nativeToolBypassRoles must be a string array")
    preflight = plan.get("commands", {}).get("preflight", {})
    path_value = preflight.get("environmentDelta", {}).get("PATH", "") if isinstance(preflight, dict) else ""
    path_entries = path_value.split(os.pathsep) if isinstance(path_value, str) else []
    executable_occurrences: list[tuple[str, bool, bool]] = []
    token_occurrences: list[tuple[str, bool, bool]] = []
    # Wrapper roles are deliberately limited to C/C++/link/archive.
    # A compiler-driver invocation for an assembly source is allowed when it
    # comes from the customer's existing CMake/toolchain behavior. Only
    # explicit ASM wrapper injection is rejected during plan validation.
    for raw_line in processes.iter_output_lines(text):
        line_analysis = analyze_native_command_text(raw_line)
        is_compile = bool(
            re.search(r"(?:^|\s)[/-]c(?:\s|$)", raw_line, flags=re.IGNORECASE)
        )
        has_assembly_source = is_compile and any(
            Path(token.strip().strip('"').strip("'")).suffix.casefold()
            in {".s", ".asm"}
            for token in line_analysis["tokens"]
        )
        executable_occurrences.extend(
            (token, is_compile, has_assembly_source)
            for token in line_analysis["executables"]
        )
        token_occurrences.extend(
            (token, is_compile, has_assembly_source)
            for token in line_analysis["tokens"]
        )
    executable_tokens = [token for token, _, _ in executable_occurrences]
    conservative_tool_tokens: list[tuple[str, bool, bool]] = []
    for token, is_compile, has_assembly_source in token_occurrences:
        assignment = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=(.*)", token)
        conservative_tool_tokens.append(
            (
                assignment.group(1) if assignment else token,
                is_compile,
                has_assembly_source,
            )
        )

    def role_matches_command(role: str, is_compile: bool) -> bool:
        if role in {"c_compiler", "cxx_compiler"}:
            return is_compile
        if role == "linker":
            return not is_compile
        return True

    verified: dict[str, str] = {}
    verified_native_tools: dict[str, str] = {}
    bypasses: list[dict[str, str]] = []
    for token, is_compile, has_assembly_source in executable_occurrences:
        token_value = token.strip().strip('"').strip("'")
        if has_assembly_source and any(
            isinstance(real, str) and pathutil.same_path(token_value, real)
            for real in real_tools.values()
        ):
            continue
        token_path = Path(token_value)
        token_name = pathutil._executable_name_key(token_path)
        for role, value in wrappers.items():
            if not isinstance(value, str):
                continue
            if not role_matches_command(role, is_compile):
                continue
            wrapper_path = Path(value)
            if token_name != pathutil._executable_name_key(wrapper_path):
                continue
            if role in native_bypass_roles:
                expected = real_tools.get(role)
                if not isinstance(expected, str) or not (
                    (token_path.is_absolute() or any(separator in token_value for separator in ("/", "\\")))
                    and pathutil.same_path(token_value, expected)
                ):
                    bypasses.append({"role": role, "executable": token_value, "expectedNativeTool": expected or ""})
                else:
                    verified_native_tools[role] = expected
                continue
            path_first = bool(path_entries) and pathutil.same_path(path_entries[0], wrapper_path.parent)
            has_path = token_path.is_absolute() or any(separator in token_value for separator in ("/", "\\"))
            if has_path and pathutil.same_path(token_value, wrapper_path):
                verified[role] = value
            elif not has_path and path_first:
                verified[role] = value
            else:
                bypasses.append({"role": role, "executable": token_value, "expectedWrapper": value})
        for role, value in real_tools.items():
            if not isinstance(value, str):
                continue
            if not role_matches_command(role, is_compile):
                continue
            real_path = Path(value)
            if (token_path.is_absolute() or any(separator in token_value for separator in ("/", "\\"))) and pathutil.same_path(
                token_value, real_path
            ):
                if role in native_bypass_roles:
                    verified_native_tools[role] = value
                else:
                    bypasses.append({"role": role, "executable": token_value, "realTool": value})
    if bypasses:
        raise errors.ExecutorError(
            "GCC_REAL_TOOL_BYPASS",
            "Native command listing invokes a matching tool outside the selected COVER wrapper path",
            bypasses,
        )
    if conservative_native_tokens:
        # Dry-run listings contain only commands, so scan every token to catch
        # an unfamiliar launcher followed by a real tool.  Actual COVER build
        # output may also describe the native tool executed by a valid wrapper;
        # that output is checked by executable position instead.
        for token, is_compile, has_assembly_source in conservative_tool_tokens:
            token_value = token.strip().strip('"').strip("'")
            if not token_value or not (
                Path(token_value).is_absolute() or any(separator in token_value for separator in ("/", "\\"))
            ):
                continue
            if has_assembly_source and any(
                isinstance(real, str) and pathutil.same_path(token_value, real)
                for real in real_tools.values()
            ):
                continue
            for role, value in wrappers.items():
                if not isinstance(value, str) or pathutil._executable_name_key(token_value) != pathutil._executable_name_key(value):
                    continue
                if not role_matches_command(role, is_compile):
                    continue
                if role in native_bypass_roles:
                    expected = real_tools.get(role)
                    if not isinstance(expected, str) or not pathutil.same_path(token_value, expected):
                        bypasses.append({"role": role, "executable": token_value, "expectedNativeTool": expected or ""})
                    else:
                        verified_native_tools[role] = expected
                    continue
                if not pathutil.same_path(token_value, value):
                    bypasses.append({"role": role, "executable": token_value, "expectedWrapper": value})
            for role, value in real_tools.items():
                if not role_matches_command(role, is_compile):
                    continue
                if isinstance(value, str) and pathutil.same_path(token_value, value):
                    if role in native_bypass_roles:
                        verified_native_tools[role] = value
                    else:
                        bypasses.append({"role": role, "executable": token_value, "realTool": value})
    if bypasses:
        raise errors.ExecutorError(
            "GCC_REAL_TOOL_BYPASS",
            "Native command text contains a matching tool path outside the selected COVER wrapper path",
            bypasses,
        )
    missing: list[dict[str, str]] = []
    for role in roles:
        value = wrappers.get(role)
        if not isinstance(value, str):
            raise errors.ExecutorError("INVALID_GCC_PREFLIGHT", f"GCC plan has no wrapper for role {role}")
        if role not in verified:
            missing.append({"role": role, "wrapper": value})
    if missing:
        raise errors.ExecutorError(
            "GCC_WRAPPER_NOT_SELECTED",
            "Native command listing does not select every required COVER wrapper",
            missing,
        )
    missing_native = [
        {"role": role, "nativeTool": real_tools.get(role, "")}
        for role in native_bypass_roles
        if role not in verified_native_tools
    ]
    if missing_native:
        raise errors.ExecutorError(
            "GCC_NATIVE_TOOL_NOT_SELECTED",
            "Native fallback role is not selected by the command listing",
            missing_native,
        )
    return {
        "verifiedWrappers": verified,
        "verifiedNativeTools": verified_native_tools,
        "commandExecutables": executable_tokens,
    }


def verify_msbuild_wrapper_text(plan: dict[str, Any], text: str | Path) -> dict[str, Any]:
    wrapper_directory = pathutil.canonical(plan.get("wrapperDirectory", ""))
    required = plan.get("wrapperVerificationNames")
    real_tools = plan.get("realTools")
    if not isinstance(required, list) or not required or not isinstance(real_tools, dict):
        raise errors.ExecutorError("INVALID_MSBUILD_WRAPPER_EXPECTATION", "MSBuild plan has no wrapper verification contract")
    found: dict[str, str] = {}
    bypasses: list[dict[str, str]] = []
    quoted = re.compile(r'["\']([^"\'\r\n]+?\\(?:cl|link|lib)\.exe)["\']', flags=re.IGNORECASE)
    # An unquoted executable cannot contain whitespace.  Keeping the token
    # boundary strict prevents a Tracker.exe command such as
    # `Tracker.exe ... /c C:\agent\wrapper\CL.exe` from being captured as one
    # giant path beginning at Tracker.exe and falsely reported as native tool
    # bypass. Paths containing spaces are handled by the quoted expression.
    unquoted = re.compile(r'([A-Za-z]:\\[^\s"\']*\\(?:cl|link|lib)\.exe)(?=\s+(?:/|-))', flags=re.IGNORECASE)
    for raw_line in processes.iter_output_lines(text):
        line = raw_line.strip()
        lower = line.casefold()
        if not line or not (re.search(r"(?:^|\s)/c(?:\s|$)", line, flags=re.IGNORECASE) or "/out:" in lower):
            continue
        for raw_path in [*quoted.findall(line), *unquoted.findall(line)]:
            path = Path(raw_path.strip())
            name = path.name.casefold()
            if name not in required:
                continue
            if path.is_file() and pathutil.same_path(path.parent, wrapper_directory):
                found[name] = str(pathutil.canonical(path))
                continue
            expected_real = real_tools.get(name)
            if isinstance(expected_real, str) and path.is_file() and pathutil.same_path(path, expected_real):
                bypasses.append({"wrapper": name, "nativeTool": str(path)})
            else:
                bypasses.append({"wrapper": name, "unexpectedTool": str(path)})
    if bypasses:
        raise errors.ExecutorError(
            "MSBUILD_REAL_TOOL_BYPASS",
            "The coverage MSBuild invoked a required native tool outside the current Agent wrapper directory",
            bypasses,
        )
    missing = sorted(set(required) - set(found))
    if missing:
        raise errors.ExecutorError(
            "MSBUILD_WRAPPER_NOT_OBSERVED",
            "The coverage MSBuild log did not prove every required Agent wrapper invocation",
            missing,
        )
    return {"verifiedWrappers": found}
