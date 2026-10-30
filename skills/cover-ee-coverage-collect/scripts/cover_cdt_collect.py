"""Collect from the existing Eclipse CDT debug session, without GUI or a second GDB."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import cover_stm32_dump as dump
import collect_environment as environment

PLUGIN_ID = "com.qualityscroll.cover.collect"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    dump.write_json(path, value)


class Bridge:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve(strict=True) / ".metadata/.plugins" / PLUGIN_ID

    def status(self):
        path = self.root / "bridge.json"
        if not path.is_file():
            raise ValueError("CDT_PLUGIN_NOT_READY: install plugin and restart IDE before testing")
        value = read_json(path)
        if value.get("protocol") != 1 or value.get("version") != "0.0.1":
            raise ValueError(f"CDT_PROTOCOL_UNSUPPORTED: installed={value.get('protocol')}, version={value.get('version')}; required protocol=1, version=0.0.1; preserve tests and replace IDE plugin")
        age = time.time()*1000 - value["heartbeatMs"]
        if age < -5000 or age > 10000:
            raise ValueError("CDT_PLUGIN_STALE: IDE is closed or plugin stopped")
        return value

    def request(self, action, *, timeout=75, **fields):
        state = self.status()
        identifier = uuid.uuid4().hex
        lock = self.root / "client-lock.json"
        try:
            with lock.open("x", encoding="utf-8") as stream:
                json.dump({"instance": state["instance"], "requestId": identifier}, stream)
        except FileExistsError as error:
            raise ValueError("CDT_REQUEST_PENDING: inspect status; do not automatically retry") from error
        folder = self.root / "requests" / identifier
        submitted = False
        completed = False
        try:
            folder.mkdir()
            request = dict(fields, action=action, instance=state["instance"], createdMs=int(time.time()*1000))
            write_json(folder / "request.json", request)
            submitted = True
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                response = folder / "response.json"
                if response.exists():
                    result = read_json(response)
                    if result.get("instance") != state["instance"] or result.get("requestId") != identifier:
                        raise ValueError("CDT_RESPONSE_MISMATCH")
                    completed = True
                    if result.get("ok") is not True:
                        raise ValueError(result.get("error", "CDT_REQUEST_FAILED"))
                    return result, folder
                if self.status()["instance"] != state["instance"]:
                    raise ValueError("IDE_RESTARTED_DURING_REQUEST")
                time.sleep(0.1)
            raise ValueError(f"CDT_REQUEST_TIMEOUT: inspect IDE and {folder}; do not retry")
        finally:
            # An unfinished request can still suspend/read. Preserve its lock as evidence.
            if completed or not submitted:
                lock.unlink(missing_ok=True)

    def recover_lock(self):
        """Explicit recovery only, after the caller has checked the IDE state."""
        state = self.status()
        lock = self.root / "client-lock.json"
        pending = read_json(lock)
        identifier = pending["requestId"]
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("Invalid pending request ID")
        response = self.root / "requests" / identifier / "response.json"
        if state["instance"] == pending["instance"] and not response.exists():
            raise ValueError("Request may still be running; lock preserved")
        lock.unlink()
        return {"status": "CLIENT_LOCK_RELEASED", "requestId": identifier}

    def acknowledge(self, folder):
        """Called only after a completed response is durably retained locally."""
        folder = Path(folder).resolve(strict=True)
        if folder.parent != (self.root / "requests").resolve() or folder.is_symlink():
            raise ValueError("Invalid request folder")
        write_json(folder / "ack.json", {"requestId": folder.name})


def doctor(project=None, workspace=None, agent_dir=None, monitoring_path=None, java_path=None):
    result = environment.discover(project, workspace)
    result["sessions"] = []
    result["checks"] = []
    for candidate in result["workspaces"]:
        try:
            bridge = Bridge(candidate)
            state = bridge.status()
            listing, folder = bridge.request("sessions")
            result["sessions"].extend(dict(s, workspace=candidate) for s in listing["sessions"])
            # No memory dump exists for session-list requests.
            bridge.acknowledge(folder)
            result["checks"].append({"workspace": candidate, "plugin": state, "status": "READY"})
        except (OSError, ValueError, KeyError) as error:
            result["checks"].append({"workspace": candidate, "status": "BLOCKED", "error": str(error)})
    try:
        if not agent_dir: raise ValueError("Agent path required")
        agent, coverage, java = dump.agent_paths(agent_dir, monitoring_path, java_path)
        result["agent"] = {"installPath": str(agent), "monitoringPath": str(coverage), "javaPath": str(java),
                           "writableHint": os.access(coverage, os.W_OK)}
    except (OSError, ValueError) as error: result["agent"] = {"error": str(error)}
    return result


def select_session(sessions, project=None, elf=None, session_id=None):
    candidates = list(sessions)
    if session_id:
        candidates = [s for s in candidates if s.get("sessionId") == session_id]
    if project:
        project = Path(project).resolve(strict=True)
        def belongs(item):
            # STM32CubeMX often stores .project one directory below the supplied project.
            if item.get("projectPath"):
                path = Path(item["projectPath"]).resolve()
                return path == project or path.is_relative_to(project)
            return item.get("elf") and Path(item["elf"]).resolve().is_relative_to(project)
        candidates = [s for s in candidates if belongs(s)]
    if elf:
        elf = Path(elf).resolve(strict=True)
        candidates = [s for s in candidates if s.get("elf") and Path(s["elf"]).resolve() == elf]
    if len(candidates) != 1:
        raise ValueError("CDT_SESSION_SELECTION_REQUIRED: use sessions output and specify --project, --elf or --session-id")
    selected = candidates[0]
    if selected.get("unavailable") or not selected.get("elf"):
        raise ValueError("CDT_SESSION_UNAVAILABLE: " + selected.get("unavailable", "ELF missing"))
    return selected


def collect(workspace, agent_dir, work_dir, project=None, elf=None, session_id=None,
            *, monitoring_path=None, java_path=None, capture_timeout=60):
    if not 10 <= capture_timeout <= 600: raise ValueError("capture-timeout must be 10..600 seconds")
    agent, coverage, java = dump.agent_paths(agent_dir, monitoring_path, java_path)
    bridge = Bridge(workspace)
    if bridge.root.resolve().is_relative_to(coverage.resolve()):
        raise ValueError("IDE bridge must be outside Agent coverage")
    listing, listing_folder = bridge.request("sessions")
    selected = select_session(listing["sessions"], project, elf, session_id)
    if selected.get("captureSupported") is False:
        raise ValueError("CDT_TARGET_UNSUPPORTED: " + selected.get("reason", "Unsupported target topology"))
    # Verify permission before suspending. The probe is not a CVD file.
    import tempfile
    with tempfile.TemporaryFile(dir=coverage): pass
    prepared = dump.prepare(work_dir, selected["elf"], agent, manual=False, monitoring_path=coverage, java_path=java)
    capture = Path(prepared["captureDirectory"])
    manifest = read_json(capture / "capture.json")
    write_json(capture / "session.json", selected)
    bridge.acknowledge(listing_folder)
    try:
        result, folder = bridge.request("capture", sessionId=selected["sessionId"],
                                        elf=manifest["elf"], elfSha256=manifest["elfSha256"],
                                        captureTimeoutSeconds=capture_timeout, timeout=capture_timeout+15)
    except Exception as error:
        write_json(capture / "capture-error.json", {"error": str(error), "targetStopped": None,
                                                   "note": "Inspect IDE state; do not automatically capture again"})
        raise
    write_json(capture / "cdt-response.json", result)
    if (result.get("sessionId") != selected["sessionId"] or result.get("elfSha256") != manifest["elfSha256"]
            or Path(result["elf"]).resolve() != Path(manifest["elf"])
            or result.get("targetStopped") is not True or result.get("symbol") != "sdump_buf"):
        raise ValueError("CDT_CAPTURE_IDENTITY_MISMATCH")
    raw = folder / "memory.bin"
    data = raw.read_bytes()
    if len(data) != result["bytes"] or hashlib.sha256(data).hexdigest() != result["sha256"]:
        raise ValueError("CDT_CAPTURE_BYTES_MISMATCH")
    with (capture / "memory.bin").open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    manifest.update(expectedBytes=result["bytes"], dumpSha256=result["sha256"],
                    sessionId=selected["sessionId"], workspace=str(Path(workspace).resolve()), targetStopped=True)
    write_json(capture / "capture.json", manifest)
    write_json(capture / "capture-stage.json", {"status": "CAPTURED", "targetStopped": True})
    bridge.acknowledge(folder)
    published = dump.publish(capture)
    published.update(captureDirectory=str(capture), elf=manifest["elf"], sessionId=selected["sessionId"],
                     targetStopped=True, address=result["address"], arrayType=result["arrayType"])
    write_json(capture / "result.json", published)
    return published


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    commands.add_parser("sessions")
    commands.add_parser("recover-lock", help="Only after manually checking the IDE and pending request")
    diagnostic = commands.add_parser("doctor")
    diagnostic.add_argument("--project")
    retry = commands.add_parser("resume", help="Validate/publish an existing dump without contacting IDE")
    retry.add_argument("--capture-dir", required=True)
    capture = commands.add_parser("collect")
    capture.add_argument("--project")
    capture.add_argument("--elf")
    capture.add_argument("--session-id")
    for command in (capture, diagnostic):
        command.add_argument("--agent-dir", default=os.environ.get("CODESCROLL_TM_INSTALL_PATH"))
        command.add_argument("--monitoring-path")
        command.add_argument("--java-path")
    capture.add_argument("--work-dir")
    capture.add_argument("--capture-timeout", type=int, default=60)
    args = parser.parse_args()
    try:
        if args.action == "doctor":
            result = doctor(args.project, args.workspace, args.agent_dir, args.monitoring_path, args.java_path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.action == "resume":
            result = dump.publish(args.capture_dir, resume=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if not args.workspace:
            candidates = environment.discover(getattr(args, "project", None))["workspaces"]
            if len(candidates) != 1: raise ValueError("WORKSPACE_SELECTION_REQUIRED: use doctor and specify --workspace")
            args.workspace = candidates[0]
        bridge = Bridge(args.workspace)
        if args.action == "status":
            result = bridge.status()
            if (bridge.root / "client-lock.json").exists():
                result["pendingRequest"] = read_json(bridge.root / "client-lock.json")
        elif args.action == "sessions":
            result, folder = bridge.request("sessions")
            bridge.acknowledge(folder)
        elif args.action == "recover-lock": result = bridge.recover_lock()
        else:
            if not args.agent_dir: raise ValueError("CODESCROLL_TM_INSTALL_PATH or --agent-dir required")
            work_dir = args.work_dir or (str(Path(args.project) / ".cover-ee/measurements") if args.project else None)
            if not work_dir: raise ValueError("--project or --work-dir required")
            result = collect(args.workspace, args.agent_dir, work_dir, args.project, args.elf, args.session_id,
                             monitoring_path=args.monitoring_path, java_path=args.java_path, capture_timeout=args.capture_timeout)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"COVER collect failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
