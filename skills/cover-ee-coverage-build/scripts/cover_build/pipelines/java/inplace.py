"""Standalone, reusable transaction for builds with fixed Java source paths.

JACOV writes to staging first. Only known staged bytes are installed temporarily,
so restoration never silently overwrites a concurrent customer edit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import uuid


class SourceError(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_path(root):
    return Path(root) / ".cover-build" / "java-inplace-active"


def assert_restored(root, *, check_lock=True):
    directory = state_path(root)
    if check_lock and directory.with_suffix(".lock").exists():
        raise SourceError("JAVA_SOURCE_RESTORE_REQUIRED: a source transaction is active or was interrupted: " + str(directory))
    if not directory.exists():
        return
    try:
        restored = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["status"] == "RESTORED"
    except (OSError, ValueError, KeyError):
        restored = False
    if not restored:
        raise SourceError("JAVA_SOURCE_RESTORE_REQUIRED: inspect the pending source backup before any build: " + str(directory))


def source_directories(root, values):
    if not isinstance(values, list) or not values:
        raise SourceError("sourceRoots must contain project-relative Java source directories")
    result = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise SourceError("Invalid source root")
        relative = Path(value)
        path = (root / relative).resolve(strict=True)
        if (relative.is_absolute() or ".." in relative.parts or path == root
                or not path.is_relative_to(root) or not path.is_dir()
                or any(p in {".git", ".cover-build", ".cover-ee", ".cover_backup"} for p in relative.parts)
                or any(path.is_relative_to(p) or p.is_relative_to(path) for p in result)):
            raise SourceError("Source roots must be distinct, non-overlapping directories below projectRoot")
        result.append(path)
    return result


def sources(directory):
    result = []
    for base, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            item = Path(base) / name
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise SourceError("Linked source-tree entries are not supported: " + str(item))
        for name in files:
            path = Path(base) / name
            if path.suffix.lower() == ".java":
                if path.stat().st_nlink != 1:
                    raise SourceError("Hard-linked Java sources are not supported: " + str(path))
                result.append(path)
    if not result:
        raise SourceError("No Java sources: " + str(directory))
    return sorted(result)


def atomic_bytes(path, data, mode):
    temporary = path.with_name(path.name + ".cover-write-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def transaction(plan, instrument, build):
    root = Path(plan["projectRoot"]).resolve(strict=True)
    assert_restored(root)
    lock = state_path(root).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    try:
        assert_restored(root, check_lock=False)
        return _transaction(plan, instrument, build)
    finally:
        lock.unlink()


def _transaction(plan, instrument, build):
    root = Path(plan["projectRoot"]).resolve(strict=True)
    roots = source_directories(root, plan["sourceRoots"])
    originals = [(directory, sources(directory)) for directory in roots]
    work = state_path(root)
    if work.exists():
        work.rename(work.with_name("java-inplace-restored-" + uuid.uuid4().hex))
    work.mkdir(parents=True, exist_ok=False)
    records = []
    manifest = {"status": "BACKING_UP", "files": records}

    def save(status):
        manifest["status"] = status
        atomic_bytes(work / "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"), 0o600)

    save("BACKING_UP")
    try:
        for index, (directory, paths) in enumerate(originals):
            for path in paths:
                info = path.stat()
                backup = work / "backup" / str(index) / path.relative_to(directory)
                backup.parent.mkdir(parents=True, exist_ok=True)
                raw = path.read_bytes()
                backup.write_bytes(raw)
                records.append({"path": str(path), "backup": str(backup),
                                "sha256": hashlib.sha256(raw).hexdigest(),
                                "mode": stat.S_IMODE(info.st_mode), "mtimeNs": info.st_mtime_ns,
                                "staged": str(work / "staged" / str(index) / path.relative_to(directory)),
                                "installed": False})
        save("STAGING")
        for index, (directory, paths) in enumerate(originals):
            output = work / "staged" / str(index)
            instrument(directory, output)
            if {p.relative_to(output) for p in sources(output)} != {p.relative_to(directory) for p in paths}:
                raise SourceError("JACOV output source set differs from the original")
            if sources(directory) != paths:
                raise SourceError("Source set changed during instrumentation")
        # Validate the whole set before changing the first source.
        for record in records:
            if digest(Path(record["path"])) != record["sha256"]:
                raise SourceError("Source changed during instrumentation: " + record["path"])
            record["stagedSha256"] = digest(Path(record["staged"]))
            record["installed"] = True
        # Save all replacement intents once. Unchanged original hashes are also
        # safe to restore after a partial replacement or abrupt process exit.
        save("APPLYING")
        for record in records:
            path = Path(record["path"])
            if path.resolve() != path or digest(path) != record["sha256"]:
                raise SourceError("Source changed before replacement: " + str(path))
            atomic_bytes(path, Path(record["staged"]).read_bytes(), record["mode"])
        save("BUILDING")
        result = build()
        for directory, paths in originals:
            if sources(directory) != paths:
                raise SourceError("Build changed the Java source set; review source-generation order")
        return result
    finally:
        conflicts = []
        for record in records:
            if not record["installed"]:
                continue
            path, backup = Path(record["path"]), Path(record["backup"])
            try:
                current = digest(path)
                if path.resolve() != path or current not in {record["sha256"], record["stagedSha256"]}:
                    raise SourceError("Concurrent source change")
                if digest(backup) != record["sha256"]:
                    raise SourceError("Backup changed")
                if current != record["sha256"]:
                    atomic_bytes(path, backup.read_bytes(), record["mode"])
                    os.utime(path, ns=(path.stat().st_atime_ns, record["mtimeNs"]))
                if digest(path) != record["sha256"]:
                    raise SourceError("Restoration hash mismatch")
            except (OSError, SourceError):
                conflicts.append(record["path"])
        manifest["restoreConflicts"] = conflicts
        save("RESTORE_CONFLICT" if conflicts else "RESTORED")
        if conflicts:
            raise SourceError("JAVA_SOURCE_RESTORE_CONFLICT: original bytes retained in " + str(work))


def run_command(argv, cwd, timeout, secrets):
    # Own the child process group so timeout/interrupt cannot leave a writer running.
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", **options)
    reader_errors = []
    def consume():
        try:
            with process.stdout:
                for line in process.stdout:
                    for secret in secrets:
                        line = line.replace(secret, "<redacted>")
                    print(line, end="", flush=True)
        except Exception:
            reader_errors.append(True)
    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise SourceError("Java coverage subprocess failed; exitCode=" + str(code))
    finally:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        reader.join(timeout=10)
        if reader.is_alive() or reader_errors:
            raise SourceError("Java coverage subprocess log could not be captured completely")


def main():
    try:
        plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        config = json.loads(Path(os.environ["COVER_JACOV_CONFIG"]).read_text(encoding="utf-8-sig"))
        key = config.get("key")
        if not isinstance(key, str) or not key.strip():
            raise SourceError("Invalid JACOV configuration key")
        command = plan["build"]
        timeout = command["timeoutSeconds"]
        def instrument(source, output):
            run_command([plan["javaExecutable"], "-jar", plan["jacovTool"], str(source), "--output", str(output)],
                        command["workingDirectory"], timeout, [key])
        def build():
            run_command([command["executable"], *command["arguments"]], command["workingDirectory"], timeout, [key])
        transaction(plan, instrument, build)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Do not emit exception values from JSON/config/child command input.
        print("[COVER] Java fixed-source build failed: " + (str(exc) if isinstance(exc, SourceError) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
