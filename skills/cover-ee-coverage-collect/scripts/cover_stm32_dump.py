"""Prepare an IDE GDB dump, then validate and publish the captured CVD.

This helper never starts, reconnects, resets, or resumes the target.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from contextlib import contextmanager


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def agent_paths(agent_dir, monitoring_path=None, java_path=None):
    agent = Path(agent_dir).resolve(strict=True)
    coverage = Path(monitoring_path).resolve(strict=True) if monitoring_path else agent / "coverage"
    java = Path(java_path).resolve(strict=True) if java_path else agent / "jre/bin" / ("java.exe" if os.name == "nt" else "java")
    jar = agent / "bin/cover-cvd-utility.jar"
    if not coverage.is_dir() or not java.is_file() or not jar.is_file():
        raise ValueError("Agent monitoring directory, Java or CVD validator is missing")
    return agent, coverage, java


def validate_cvd(raw, agent_dir, log_path, java_path=None):
    """Delegate CVD interpretation to the installed product's VIEW mode."""
    agent_dir = Path(agent_dir)
    java = Path(java_path) if java_path else agent_dir / "jre" / "bin" / ("java.exe" if os.name == "nt" else "java")
    jar = agent_dir / "bin" / "cover-cvd-utility.jar"
    if not java.is_file() or not jar.is_file():
        raise ValueError("Agent Java runtime or cover-cvd-utility.jar is missing")
    with Path(log_path).open("wb") as log:
        try:
            result = subprocess.run(
                [str(java), "-jar", str(jar), "-m", "VIEW", "-i", str(raw)],
                stdout=log, stderr=subprocess.STDOUT, timeout=60, check=False)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"CVD utility timed out; see {log_path}") from error
    if result.returncode != 0:
        raise ValueError(f"CVD utility rejected capture (exit {result.returncode}); see {log_path}")
    return {"validator": str(jar), "validatorExitCode": result.returncode,
            "validationLog": str(log_path)}


def gdb_path(path):
    value = Path(path).resolve().as_posix()
    if any(c.isspace() or c in '\"\'' for c in value):
        raise ValueError("Use a capture work directory without spaces or quotes")
    return value


def prepare(work_dir, elf, agent_dir, *, manual=True, monitoring_path=None, java_path=None):
    elf = Path(elf).resolve(strict=True)
    agent_dir = Path(agent_dir).resolve(strict=True)
    if not elf.is_file() or not agent_dir.is_dir():
        raise ValueError("ELF file and Agent directory are required")
    capture = Path(work_dir).resolve() / str(uuid.uuid4())
    # A partial capture must never appear in the Agent watch directory.
    coverage = Path(monitoring_path).resolve(strict=True) if monitoring_path else agent_dir / "coverage"
    if capture.is_relative_to(coverage.resolve()):
        raise ValueError("Capture directory must be outside Agent coverage")
    raw = capture / "memory.bin"
    script = capture / "dump.gdb"
    if manual:
        gdb_path(raw)
    capture.mkdir(parents=True, exist_ok=False)
    # 'frame' fails when there is no stopped stack. No attach/load/reset commands.
    # Array value export preserves sizeof(sdump_buf), including trailing capacity.
    if manual:
        script.write_text(
        "# Run in the existing, suspended IDE GDB session after checking its ELF.\n"
        "frame\n"
        "info files\n"
        "printf \"COVER buffer address: %p, array bytes: %u\\n\", &sdump_buf, (unsigned int)sizeof(sdump_buf)\n"
        f"dump binary value {gdb_path(raw)} sdump_buf\n"
        "echo COVER_DUMP_SAVED\\n\n", encoding="utf-8")
    manifest = {"schema": "cover-stm32-dump/v1", "elf": str(elf),
                "elfSha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
                "agentInstallPath": str(agent_dir),
                "javaPath": str(Path(java_path).resolve(strict=True)) if java_path else None,
                "coverageDirectory": str(coverage), "symbol": "sdump_buf"}
    (capture / "capture.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"captureDirectory": str(capture), "expectedElf": str(elf),
            "gdbCommand": "source " + gdb_path(script) if manual else None,
            "status": "WAITING_FOR_IDE_DUMP" if manual else "WAITING_FOR_CDT_DUMP"}


@contextmanager
def publication_lock(capture):
    # OS locks are released on process death. Keep the inode/path to avoid
    # unlink/open races between publishers; file existence is not lock ownership.
    with (Path(capture) / "publish.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("PUBLISH_PENDING: another publisher holds the OS lock") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt": msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def publish(capture_dir, *, resume=False):
    capture = Path(capture_dir).resolve(strict=True)
    with publication_lock(capture):
        try:
            return _publish(capture, resume=resume)
        except Exception as error:
            write_json(capture / "publish-error.json", {"error": str(error), "captureDirectory": str(capture)})
            raise


def _publish(capture, *, resume):
    report_path = capture / "result.json"
    if report_path.exists():
        if resume:
            return json.loads(report_path.read_text(encoding="utf-8"))
        raise ValueError("This capture was already published; prepare a new capture")
    manifest = json.loads((capture / "capture.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "cover-stm32-dump/v1":
        raise ValueError("Unknown capture schema")
    elf = Path(manifest["elf"])
    if hashlib.sha256(elf.read_bytes()).hexdigest() != manifest["elfSha256"]:
        raise ValueError("ELF changed since preparation; capture again with matching firmware")
    coverage = Path(manifest["coverageDirectory"]).resolve(strict=True)
    if not coverage.is_dir():
        raise ValueError("Agent coverage directory does not exist")
    raw = capture / "memory.bin"
    data = raw.read_bytes()
    if not data:
        raise ValueError("Captured memory file is empty")
    if "expectedBytes" in manifest and len(data) != manifest["expectedBytes"]:
        raise ValueError("Captured file size differs from CDT array size")
    if "dumpSha256" in manifest and hashlib.sha256(data).hexdigest() != manifest["dumpSha256"]:
        raise ValueError("Captured file differs from CDT response")
    journal_path = capture / "delivery.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else None
    digest = hashlib.sha256(data).hexdigest()
    if journal:
        if journal["sha256"] != digest or Path(journal["destination"]).parent != coverage:
            raise ValueError("DELIVERY_RECORD_MISMATCH")
        destination = Path(journal["destination"])
        if journal["status"] == "PUBLISHED":
            write_json(report_path, journal["report"])
            return journal["report"]
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            journal["status"] = "PUBLISHED"
            write_json(journal_path, journal)
            write_json(report_path, journal["report"])
            return journal["report"]
        # The Agent may already have consumed the file after a process crash.
        raise ValueError("DELIVERY_UNCERTAIN: do not publish again; inspect Agent and delivery.json")
    agent = Path(manifest.get("agentInstallPath", coverage.parent))
    java = manifest.get("javaPath")
    validation_path = capture / "validation.json"
    fingerprint = {"dump": digest, "validator": hashlib.sha256((agent / "bin/cover-cvd-utility.jar").read_bytes()).hexdigest(),
                   "java": str(java or agent / "jre/bin" / ("java.exe" if os.name == "nt" else "java"))}
    previous = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else None
    if previous and previous.get("fingerprint") == fingerprint:
        report = previous["report"]
    else:
        report = validate_cvd(raw, agent, capture / "cvd-view.log", java_path=java)
        write_json(validation_path, {"fingerprint": fingerprint, "report": report})
    if raw.read_bytes() != data:
        raise ValueError("Captured file changed during validation; capture again")
    report.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    token = uuid.uuid4().hex
    temporary = coverage / ("cover-" + token + ".partial")
    destination = coverage / ("cover-" + token + ".cvd")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    report.update(status="CVD_PUBLISHED", destination=str(destination),
                  serverMeasurementVerified=False)
    report.update(captureDirectory=str(capture), elf=str(elf))
    for name in ("sessionId", "targetStopped", "workspace"):
        if name in manifest: report[name] = manifest[name]
    journal = {"status": "PUBLISHING", "destination": str(destination), "sha256": digest, "report": report}
    write_json(journal_path, journal)
    # Journal precedes the irreversible handoff; ambiguous crashes never resend.
    temporary.rename(destination)
    journal["status"] = "PUBLISHED"
    write_json(journal_path, journal)
    write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--work-dir", required=True)
    prep.add_argument("--elf", required=True)
    prep.add_argument("--agent-dir", default=os.environ.get("CODESCROLL_TM_INSTALL_PATH"))
    prep.add_argument("--monitoring-path")
    prep.add_argument("--java-path")
    send = commands.add_parser("publish")
    send.add_argument("--capture-dir", required=True)
    send.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            if not args.agent_dir:
                raise ValueError("--agent-dir or CODESCROLL_TM_INSTALL_PATH required")
            result = prepare(args.work_dir, args.elf, args.agent_dir, monitoring_path=args.monitoring_path, java_path=args.java_path)
        else:
            result = publish(args.capture_dir, resume=args.resume)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"COVER dump failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
