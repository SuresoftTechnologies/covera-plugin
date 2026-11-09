"""Evidence-backed archive policy review and explicit Agent-wide configuration changes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

from cover_build.services import agent_host
from cover_build.common import envelopes, fileio, pathutil, project_lock
from cover_build.core.errors import ExecutorError, WorkflowWaiting


def _required_mode(value):
    """Keep the required symbol scope separate from the requested configuration action."""
    has_archives = any(link.get("inputKind") in {"ARCHIVE", "MIXED"}
                       for link in value.get("links", []))
    required = value.get("requiredMode")
    if (has_archives and required not in {"tu", "module"}) or (
            required is not None and required not in {"tu", "module"}):
        raise WorkflowWaiting("ARCHIVE_REQUIRED_MODE_REQUIRED",
            "The MODEL must determine archiveReview.requiredMode (tu/module). Trace direct target "
            "objects and archive producer/consumer relationships in original commands and RSPs; "
            "startup/external objects alone do not establish coverage object input. If target objects "
            "arrive only through archives and the final EXE/DLL must be a COVER module, select tu. "
            "Otherwise evaluate independent archive modules and mixed-input ownership; do not infer "
            "mode from filenames or archiveRole alone. Explain the expected module composition and "
            "resolve conflicting target requirements before resubmitting the same job.",
            {"originalToolchainDecision": {}}, purpose="BUILD_ENVIRONMENT")
    if required is not None and value.get("mode") not in {"KEEP", required}:
        raise WorkflowWaiting("ARCHIVE_MODE_CONFLICT",
            "archiveReview.mode conflicts with requiredMode; review the module intent and resubmit "
            "originalToolchainDecision with consistent fields.",
            {"originalToolchainDecision": {}}, purpose="BUILD_ENVIRONMENT")
    return required


def review(value, evidence, lines):
    if not isinstance(value, dict):
        raise WorkflowWaiting("ARCHIVE_REVIEW_REQUIRED",
            "Include archiveReview in originalToolchainDecision: mode KEEP/tu/module, requiredMode tu/module for archive inputs, reason, "
            "moduleIntent, and links with output, kind, inputKind, archiveRole, commandText, line, "
            "responseFiles and archiveCommands. Inspect all final links; do not decide from filenames.",
            {"originalToolchainDecision": {}}, evidence=[evidence["buildLog"]["path"]])
    if value.get("mode") not in {"KEEP", "tu", "module"}:
        raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Resolve conflicting module intent before selecting a mode")
    if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("reason", "moduleIntent")):
        raise ExecutorError("INVALID_ARCHIVE_REVIEW", "reason and moduleIntent are required")
    links = value.get("links")
    if not isinstance(links, list) or (not links and value["mode"] != "KEEP"):
        raise ExecutorError("INVALID_ARCHIVE_REVIEW", "A change requires final-link evidence")
    def citation(item):
        if not isinstance(item, dict):
            raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Command evidence must be an object")
        n, command = item.get("line"), item.get("commandText")
        if type(n) is not int or not 1 <= n <= len(lines) or not isinstance(command, str) or not command or command not in lines[n-1]:
            raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Command must cite the current original log")
        rsp = item.get("responseFiles", [])
        if not isinstance(rsp, list) or ("@" in command and not rsp):
            raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Response-file commands require response evidence")
        for entry in rsp:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Malformed response-file evidence")
            path = Path(entry["path"])
            if not path.is_absolute():
                raise ExecutorError("INVALID_ARCHIVE_REVIEW", f"Response-file path must be absolute: {path}")
            if not path.is_file():
                raise ExecutorError("INVALID_ARCHIVE_REVIEW", f"Response file is missing or not a file: {path}")
            try:
                matches = pathutil.sha256_matches_file(path, entry.get("sha256"))
            except ValueError as exc:
                raise ExecutorError("INVALID_ARCHIVE_REVIEW", f"Invalid response-file SHA-256 for {path}: {exc}") from exc
            if not matches:
                raise ExecutorError("INVALID_ARCHIVE_REVIEW", f"Response-file SHA-256 mismatch: {path}")
    for link in links:
        if (not isinstance(link, dict) or not isinstance(link.get("output"), str)
                or not Path(link["output"]).is_absolute()
                or link.get("kind") not in {"EXECUTABLE", "SHARED_LIBRARY", "STATIC_LIBRARY"}
                or link.get("inputKind") not in {"OBJECT", "ARCHIVE", "MIXED"}
                or link.get("archiveRole") not in {"NONE", "INTERMEDIATE", "INDEPENDENT", "UNKNOWN"}):
            raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Malformed link classification")
        citation(link)
        commands = link.get("archiveCommands", [])
        if not isinstance(commands, list):
            raise ExecutorError("INVALID_ARCHIVE_REVIEW", "archiveCommands must be an array")
        for command in commands:
            citation(command)
        if link["archiveRole"] == "INTERMEDIATE" and not commands:
            raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Intermediate archives require their creation commands")
        if link["archiveRole"] == "UNKNOWN" and value["mode"] != "KEEP":
            raise ExecutorError("INVALID_ARCHIVE_REVIEW", "Unknown archive ownership cannot authorize a change")
    _required_mode(value)
    return {**value, "evidenceDigest": evidence["integrity"]}


def _runtime(root, offset=0):
    log = root / "logs" / "testmonitor.log"
    data = log.read_bytes() if log.is_file() else b""
    text = data[offset if len(data) >= offset else 0:].decode("utf-8", errors="replace")
    matches = re.findall(r"Archive File Symbol Type:\s*(TU|MODULE)\b", text)
    if not matches:
        raise ExecutorError("ARCHIVE_OPTION_SUPPORT_UNCONFIRMED", "No installed Agent runtime evidence for archiveFileSymbolType")
    return matches[-1].lower()


def _configured(raw, runtime):
    matches = re.findall(rb"(?m)^archiveFileSymbolType:[ \t]*([^#\r\n]+)", raw)
    if len(matches) > 1:
        raise ExecutorError("ARCHIVE_CONFIG_AMBIGUOUS", "Duplicate archiveFileSymbolType settings")
    if not matches:
        return runtime  # Omitted default is taken from this installed Agent's startup record.
    value = matches[0].strip().strip(b"\"'").decode("ascii").lower()
    if value not in {"tu", "module"}:
        raise ExecutorError("ARCHIVE_CONFIG_INVALID", "Unsupported archiveFileSymbolType value")
    return value


def _assert_idle(descriptor):
    # Conservative process scan; inspection failure must not permit a restart.
    if os.name == "nt":
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "@(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object Name,CommandLine) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, check=True, timeout=30)
        rows = json.loads(result.stdout or "[]")
        if isinstance(rows, dict): rows = [rows]
        commands = [(r.get("Name", "") + " " + (r.get("CommandLine") or "")) for r in rows]
    else:
        result = subprocess.run(["ps", "-axo", "comm=,args="], capture_output=True, text=True, check=True, timeout=30)
        commands = result.stdout.splitlines()
    busy = re.compile(r"(?i)(?:^|[ /\\])(?:cs(?:gcc|g\+\+|ar|cl|link)|br_(?:gcc|win|cl)|"
                      r"msbuild|cmake|ninja|make|mingw32-make)(?:\.exe)?(?:[ \t]|$)")
    if any(busy.search(command) for command in commands):
        raise WorkflowWaiting("AGENT_BUSY", "Another build may be using this Agent; wait before resuming", {},
                              decision_owner="MODEL", purpose="BUILD_ENVIRONMENT")


def _restart(job, descriptor):
    path = Path(job["directories"]["temporary"]) / "archive-agent.json"
    fileio.atomic_write_json(path, fileio.add_integrity({k:v for k,v in descriptor.items() if k != "integrity"}))
    from cover_build.services.agent_local import c_build_listener_port
    args = SimpleNamespace(descriptor=str(path), allow_start=True,
                           port=[c_build_listener_port(), 9091, 9092, 9093, 9094, 12345], timeout=45, hidden=False)
    if agent_host._installed_agent_processes(descriptor):
        agent_host.stop_agent_for_mapping_refresh(args)
    log = Path(descriptor["agentRoot"]) / "logs" / "testmonitor.log"
    offset = log.stat().st_size if log.is_file() else 0
    agent_host.start_agent(args)
    return _runtime(Path(descriptor["agentRoot"]), offset)


def ensure(job, descriptor, review_value, approval):
    required = _required_mode(review_value)
    root = Path(descriptor["agentRoot"])
    config = root / "bin" / "agent-config.yaml"
    with project_lock.acquire(root, agent_config=True):
        raw = config.read_bytes()
        runtime = _runtime(root)
        current = _configured(raw, runtime)
        if review_value["mode"] == "KEEP" and required is not None and (
                current != required or runtime != required):
            raise WorkflowWaiting("ARCHIVE_KEEP_CONFLICT",
                f"KEEP cannot satisfy requiredMode={required}: configured={current}, runtime={runtime}. "
                "Review the module intent and resubmit originalToolchainDecision with an explicit "
                "mode or corrected evidence. Agent-wide changes still require authorization.",
                {"originalToolchainDecision": {}}, purpose="BUILD_ENVIRONMENT",
                evidence=[str(config), str(Path(job["directories"]["temporary"]) / "validated-original-toolchain.json")])
        desired = current if review_value["mode"] == "KEEP" else review_value["mode"]
        state_path = Path(job["directories"]["temporary"]) / "archive-policy-state.json"
        state = fileio.load_json(state_path) if state_path.is_file() else {}
        result = {"agentRoot": str(root), "configured": current, "runtime": runtime,
                  "requiredMode": desired, "review": review_value, "scope": "AGENT_WIDE"}
        if current == desired and runtime == desired:
            result.update(status="APPLIED" if state.get("status") in {"APPLIED", "APPLYING"} else "UNCHANGED", backup=state.get("backup"))
        else:
            proposal = {"jobId": job["jobId"], "agentRoot": str(root), "configSha256": hashlib.sha256(raw).hexdigest(),
                        "from": current, "runtime": runtime, "to": desired,
                        "reviewDigest": fileio.sha256_json(review_value), "lifetime": "WORKFLOW_INVOCATION"}
            pending = (state.get("status") == "APPLYING" and state.get("requiredMode") == desired
                       and state.get("writtenSha256") == proposal["configSha256"]
                       and state.get("proposal", {}).get("reviewDigest") == proposal["reviewDigest"])
            expected_approval = state["proposal"] if pending else proposal
            if not isinstance(approval, dict) or approval.get("approved") is not True or any(approval.get(k) != v for k,v in expected_approval.items()):
                raise WorkflowWaiting("ARCHIVE_POLICY_APPROVAL_REQUIRED",
                    "Temporarily change archiveFileSymbolType and restart this Agent. Restore the original configuration on exit, including failure or waiting. This affects every project using the Agent.",
                    {"archivePolicyApproval": expected_approval}, decision_owner="USER", purpose="CUSTOMER_CONFIGURATION")
            if descriptor.get("mode") != "PROCESS_CONTROLLER":
                raise ExecutorError("ARCHIVE_RESTART_UNSUPPORTED", "Automatic archive policy changes require process-controller mode")
            _assert_idle(descriptor)
            backup = Path(job["directories"]["temporary"]) / ("agent-config." + proposal["configSha256"] + ".bak")
            rollback_raw = raw
            if pending:
                backup = Path(state["backup"])
                rollback_raw = backup.read_bytes()
                if hashlib.sha256(rollback_raw).hexdigest() != state["beforeSha256"]:
                    raise ExecutorError("ARCHIVE_BACKUP_CHANGED", "Pending rollback backup changed")
            elif not backup.exists():
                backup.write_bytes(raw)
            elif backup.read_bytes() != raw:
                raise ExecutorError("ARCHIVE_BACKUP_CHANGED", "Existing backup does not match the original config")
            if re.search(rb"(?m)^archiveFileSymbolType:", raw):
                updated = re.sub(rb"(?m)^archiveFileSymbolType:[^\r\n]*", b"archiveFileSymbolType: " + desired.encode(), raw)
            else:
                updated = raw + b"\narchiveFileSymbolType: " + desired.encode() + b"\n"
            journal = {**result, "status": "APPLYING", "backup": str(backup),
                       "proposal": expected_approval, "beforeSha256": hashlib.sha256(rollback_raw).hexdigest(),
                       "writtenSha256": hashlib.sha256(updated).hexdigest(),
                       "restoreRequired": True, "descriptor": descriptor}
            fileio.atomic_write_json(state_path, journal)
            try:
                if config.read_bytes() != raw:
                    raise ExecutorError("ARCHIVE_CONFIG_CONFLICT", "Agent config changed concurrently")
                temporary = config.with_name(config.name + "." + job["jobId"] + ".tmp")
                temporary.write_bytes(updated)
                os.replace(temporary, config)
                if _restart(job, descriptor) != desired:
                    raise ExecutorError("ARCHIVE_POLICY_NOT_APPLIED", "Restarted Agent did not report the requested archive mode")
                result.update(journal)
                result.update(status="APPLIED", configured=desired, runtime=desired, backup=str(backup))
            except Exception as exc:
                if config.read_bytes() == updated:
                    restore_path = config.with_name(config.name + "." + job["jobId"] + ".restore")
                    restore_path.write_bytes(rollback_raw)
                    os.replace(restore_path, config)
                    try:
                        restored_mode = _restart(job, descriptor)
                        if restored_mode != _configured(rollback_raw, expected_approval["runtime"]):
                            raise ExecutorError("ARCHIVE_ROLLBACK_NOT_APPLIED", "Restored Agent reports a different mode")
                        journal["status"] = "ROLLED_BACK"
                        journal["restoreRequired"] = False
                    except Exception as recovery:
                        journal["status"] = "RECOVERY_FAILED"
                        journal["recoveryError"] = str(recovery)
                else:
                    journal["status"] = "CONFLICT_REQUIRES_RECOVERY"
                fileio.atomic_write_json(state_path, journal)
                raise ExecutorError("ARCHIVE_POLICY_APPLY_FAILED", str(exc), [str(state_path)]) from exc
        fileio.atomic_write_json(state_path, result)
        job["archivePolicy"] = {"agentRoot": str(root), "requiredMode": desired, "lifetime": "WORKFLOW_INVOCATION"}
        return envelopes.envelope("APPLYING_ARCHIVE_POLICY", "SUCCEEDED", result)


def restore(job):
    """Restore only a configuration owned by this invocation; never hide build results."""
    path = Path(job["directories"]["temporary"]) / "archive-policy-state.json"
    if not path.is_file():
        return None
    state = fileio.load_json(path)
    if not state.get("restoreRequired"):
        return state
    try:
        root = Path(state["agentRoot"])
        with project_lock.acquire(root, agent_config=True):
            config = root / "bin" / "agent-config.yaml"
            before = Path(state["backup"]).read_bytes()
            if hashlib.sha256(before).hexdigest() != state["beforeSha256"]:
                raise ExecutorError("ARCHIVE_BACKUP_CHANGED", "Restore backup changed")
            raw = config.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest not in {state["writtenSha256"], state["beforeSha256"]}:
                raise ExecutorError("ARCHIVE_CONFIG_CONFLICT", "Agent configuration changed externally; not overwritten")
            descriptor = state["descriptor"]
            _assert_idle(descriptor)
            state["restoration"] = {"status": "RESTORING"}
            fileio.atomic_write_json(path, state)
            if raw != before:
                temporary = config.with_name(config.name + "." + job["jobId"] + ".restore")
                temporary.write_bytes(before)
                if config.read_bytes() != raw:
                    raise ExecutorError("ARCHIVE_CONFIG_CONFLICT", "Agent configuration changed before restoration")
                os.replace(temporary, config)
            mode = _restart(job, descriptor)
            if mode != _configured(before, state["proposal"]["runtime"]):
                raise ExecutorError("ARCHIVE_RESTORE_NOT_APPLIED", "Agent did not report its original mode")
            state.update(restoreRequired=False)
            state["restoration"] = {"status": "RESTORED", "runtime": mode}
    except Exception as exc:
        state["restoration"] = {"status": "FAILED", "errorCode": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
    fileio.atomic_write_json(path, state)
    return state
