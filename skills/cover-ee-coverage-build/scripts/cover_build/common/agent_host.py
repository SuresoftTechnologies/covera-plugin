"""Local Agent discovery, identity bootstrap, TLS transport, and start-up on this host."""

from __future__ import annotations

import argparse
import hashlib
import locale
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Iterable

from cover_build.common import envelopes
from cover_build.common import fileio
from cover_build.common import jobs
from cover_build.common import pathutil
from cover_build.common import project_files
from cover_build.common import servers
from cover_build.core import errors


AGENT_SCHEMA = "cover-agent-descriptor/v1"


MAC_PATTERN = re.compile(r"^([0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}$")


MAC_BYTES_PATTERN = re.compile(rb"(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}")


def read_hardware_id_file(path: Path) -> str:
    matches = sorted(
        {
            match.group(0).decode("ascii").upper()
            for match in MAC_BYTES_PATTERN.finditer(path.read_bytes())
        }
    )
    if len(matches) != 1:
        raise errors.ExecutorError(
            "INVALID_HARDWARE_ID",
            f"Expected one serialized hardware ID in {path}; found {len(matches)}",
        )
    return matches[0]


AGENT_TLS_TRUSTSTORE_PASSWORD = "changeit"


def prepare_agent_tls_transport(job_context: str | None, server_url: str, java: Path) -> dict[str, Any] | None:
    if not job_context:
        return None
    job = jobs.load_job_context(job_context)
    agent_origin = servers.job_agent_server_origin(job)
    if servers.server_origin(server_url).casefold() != agent_origin.casefold():
        raise errors.ExecutorError(
            "AGENT_SERVER_ORIGIN_MISMATCH",
            "The Local Agent server URL does not match the origin recorded in the protected job",
        )
    parsed = urllib.parse.urlsplit(agent_origin)
    if parsed.scheme.casefold() == "http":
        return None
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise errors.ExecutorError("INSECURE_TLS_REQUIRES_HTTPS", "Local Agent TLS adaptation requires an HTTPS server")
    if job.get("insecureSkipTlsVerify") is not True:
        return None
    host = parsed.hostname
    port = parsed.port or 443
    temporary_root = pathutil.canonical(job["directories"]["temporary"])
    tls_root = pathutil.canonical(temporary_root / "agent-tls", False)
    if not pathutil.path_within(tls_root, temporary_root):
        raise errors.ExecutorError("INVALID_JOB_CONTEXT", "Local Agent TLS files escaped the protected job directory")
    tls_root.mkdir(parents=True, exist_ok=True)
    certificate_path = tls_root / "server.cer"
    truststore_path = tls_root / "truststore.p12"
    truststore_next = tls_root / f"truststore.{uuid.uuid4().hex}.p12"

    if certificate_path.is_file():
        certificate = certificate_path.read_bytes()
        certificate_source = "REUSED_JOB_FIRST_PRESENTED_CERTIFICATE"
    else:
        try:
            context = ssl._create_unverified_context()
            with socket.create_connection((host, port), timeout=10) as connection:
                with context.wrap_socket(connection, server_hostname=host) as tls:
                    certificate = tls.getpeercert(binary_form=True)
        except (OSError, ssl.SSLError) as exc:
            raise errors.ExecutorError(
                "AGENT_TLS_CERTIFICATE_FETCH_FAILED",
                f"Cannot fetch the Local Agent server certificate from {host}:{port}: {exc}",
            ) from exc
        if not certificate:
            raise errors.ExecutorError("AGENT_TLS_CERTIFICATE_FETCH_FAILED", "The COVER server did not present a certificate")
        fileio.atomic_write_bytes(certificate_path, certificate)
        certificate_source = "UNVERIFIED_FIRST_PRESENTED_CERTIFICATE"

    keytool = java.with_name("keytool.exe" if os.name == "nt" else "keytool")
    if not keytool.is_file():
        raise errors.ExecutorError("AGENT_KEYTOOL_NOT_FOUND", f"The packaged Agent keytool is missing: {keytool}")
    imported = subprocess.run(
        [
            str(keytool), "-importcert", "-noprompt", "-alias", "cover-job-server",
            "-file", str(certificate_path), "-keystore", str(truststore_next),
            "-storetype", "PKCS12", "-storepass", AGENT_TLS_TRUSTSTORE_PASSWORD,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        shell=False,
        check=False,
    )
    if imported.returncode != 0 or not truststore_next.is_file():
        if truststore_next.exists():
            truststore_next.unlink()
        raise errors.ExecutorError(
            "AGENT_TRUSTSTORE_CREATE_FAILED",
            f"The packaged Agent keytool exited with {imported.returncode}",
            [imported.stdout],
        )
    os.replace(truststore_next, truststore_path)
    options = [
        f"-Djavax.net.ssl.trustStore={truststore_path}",
        f"-Djavax.net.ssl.trustStorePassword={AGENT_TLS_TRUSTSTORE_PASSWORD}",
        "-Djavax.net.ssl.trustStoreType=PKCS12",
    ]
    return {
        "mode": "JOB_SCOPED_PRESENTED_CERTIFICATE_TRUST",
        "serverOrigin": agent_origin,
        "serverHost": host,
        "certificateSha256": hashlib.sha256(certificate).hexdigest().upper(),
        "certificateSource": certificate_source,
        "certificate": str(certificate_path),
        "truststore": str(truststore_path),
        "jvmOptions": options,
    }


def _agent_product_start_command(root: Path, windows: bool) -> dict[str, Any]:
    """Use the product launcher rather than reimplement its process lifecycle."""
    launcher = root / "bin" / ("run_agent_controller.cmd" if windows else "cvctl.sh")
    if not launcher.is_file():
        raise errors.ExecutorError("LOCAL_AGENT_NOT_INSTALLED", f"Missing Agent launcher: {launcher}")
    interpreter = shutil.which("cmd.exe" if windows else "bash")
    if not interpreter:
        raise errors.ExecutorError("AGENT_LAUNCHER_SHELL_NOT_FOUND", "The product Agent launcher requires cmd.exe or bash")
    return {
        "launcher": str(launcher),
        "executable": str(pathutil.canonical(interpreter)),
        "arguments": ["/d", "/v:off", "/s", "/c", '""%COVER_AGENT_START_SCRIPT%""'] if windows else [str(launcher), "start"],
        "environmentDelta": {"COVER_AGENT_START_SCRIPT": str(launcher)} if windows else {},
    }


def _prepare_windows_agent_launcher(source: Path, directory: Path) -> Path:
    """Fix the known legacy product CMD in a job copy, not in the installation."""
    raw = source.read_bytes()
    command = rb"(?im)^[ \t]*%JAVA_EXE% -jar %CODESCROLL_TM_INSTALL_PATH%\\bin\\%AGENT_CONTROLLER_JAR%[ \t]*$"
    # Unknown scripts may depend on their own location. Do not rewrite them.
    normalized = raw.replace(b"\r\n", b"\n")
    if (len(re.findall(command, normalized)) != 1
            or re.search(rb"(?i)%(?:~[^%\n ]*)?0", normalized)
            or len(re.findall(rb"(?im)^[ \t]*PAUSE[ \t]*$", normalized)) != 1
            or normalized.rstrip().split(b"\n")[-1].strip().upper() != b"PAUSE"):
        return source
    content = re.sub(command, lambda _: b'    "%JAVA_EXE%" -jar "%CODESCROLL_TM_INSTALL_PATH%\\bin\\%AGENT_CONTROLLER_JAR%"', normalized)
    content = re.sub(rb"(?im)^([ \t]*)set (JAVA_EXE|INNER_JAVA_EXE|AGENT_CONTROLLER_JAR)=(.*)$",
                     lambda m: m[1] + b'set "' + m[2] + b'=' + m[3] + b'"', content)
    content = re.sub(rb"(?im)if exist %INNER_JAVA_EXE%", lambda _: b'if exist "%INNER_JAVA_EXE%"', content)
    content = re.sub(rb"(?im)^([ \t]*)echo ([^\n\"]*%CODESCROLL_TM_(?:INSTALL_PATH|SERVER_URL)%[^\n\"]*)$",
                     lambda m: m[1] + b'echo "' + m[2] + b'"', content)
    # PAUSE would hide a JVM failure behind a running cmd.exe and a timeout.
    content = re.sub(rb"(?im)^[ \t]*PAUSE[ \t]*$", b"exit /b %errorlevel%", content)
    target = directory / "run-agent-controller.compat.cmd"
    target.write_bytes(content.replace(b"\n", b"\r\n"))
    return target


def discover_agent(args: argparse.Namespace) -> dict[str, Any]:
    explicit_root = getattr(args, "agent_root", None)
    if explicit_root:
        root_value = explicit_root
        root_source = "ARGUMENT:agent-root"
    else:
        root_value = os.environ.get("CODESCROLL_TM_INSTALL_PATH")
        root_source = "ENVIRONMENT:CODESCROLL_TM_INSTALL_PATH"
    if not root_value:
        raise errors.ExecutorError(
            "AGENT_ROOT_REQUIRED",
            "Set CODESCROLL_TM_INSTALL_PATH or provide --agent-root",
        )
    root = pathutil.canonical(root_value)
    bin_dir = root / "bin"
    required = [bin_dir / "agent-config.yaml", root / "logs", root / "wrapper"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise errors.ExecutorError("LOCAL_AGENT_NOT_INSTALLED", "Agent installation signature is incomplete", missing)
    controller_jars = list(bin_dir.glob("tm-local-agent-controller-*-exec.jar"))
    agent_jars = [path for path in bin_dir.glob("tm-local-agent-*-exec.jar") if "controller" not in path.name]
    controller_jar = project_files.find_single(controller_jars, "AMBIGUOUS_AGENT_CONTROLLER", "controller jar")
    agent_jar = project_files.find_single(agent_jars, "AMBIGUOUS_AGENT_JAR", "agent jar")
    if os.name == "nt":
        launcher = bin_dir / "run_agent_controller.cmd"
        java = root / "jre" / "bin" / "java.exe"
        if args.service_name:
            sc = pathutil.canonical(shutil.which("sc.exe") or r"C:\Windows\System32\sc.exe")
            query = subprocess.run([str(sc), "qc", args.service_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding=locale.getpreferredencoding(False), errors="replace", shell=False, check=False)
            if query.returncode != 0:
                raise errors.ExecutorError("AGENT_SERVICE_NOT_FOUND", f"Cannot query service {args.service_name}", [query.stdout])
            binary_lines = [line.split(":", 1)[1].strip() for line in query.stdout.splitlines() if "BINARY_PATH_NAME" in line and ":" in line]
            if len(binary_lines) != 1 or str(root).lower() not in binary_lines[0].lower():
                raise errors.ExecutorError("AGENT_SERVICE_PATH_MISMATCH", f"Service {args.service_name} is not rooted at {root}", binary_lines)
            executable = str(sc)
            arguments = ["start", args.service_name]
            mode = "WINDOWS_SERVICE"
        else:
            product_start = _agent_product_start_command(root, True)
            executable = product_start["executable"]
            arguments = product_start["arguments"]
            mode = "PROCESS_CONTROLLER"
    else:
        java = root / "jre" / "bin" / "java"
        product_start = _agent_product_start_command(root, False)
        launcher = Path(product_start["launcher"])
        executable = product_start["executable"]
        arguments = product_start["arguments"]
        mode = "PROCESS_CONTROLLER"
    for path in (launcher, java, controller_jar, agent_jar):
        if not path.is_file():
            raise errors.ExecutorError("LOCAL_AGENT_NOT_INSTALLED", f"Missing Agent runtime file: {path}")
    if os.name != "nt" and not os.access(java, os.X_OK):
        raise errors.ExecutorError(
            "AGENT_RUNTIME_NOT_EXECUTABLE",
            "The packaged Agent Java runtime is not executable",
            [str(java)],
        )
    agent_origin = servers.server_origin(args.server_url)
    tls_transport = prepare_agent_tls_transport(getattr(args, "job_context", None), agent_origin, java)
    if tls_transport and args.service_name:
        raise errors.ExecutorError(
            "INSECURE_TLS_AGENT_SERVICE_UNSUPPORTED",
            "A Windows service cannot receive the job-scoped Local Agent TLS settings; use process-controller mode",
        )
    hardware_file = Path.home() / ".cstm_HardwareAddress"
    hardware_id = None
    if hardware_file.is_file():
        hardware_id = read_hardware_id_file(hardware_file)
    log_hardware_ids: set[str] = set()
    for log_name in ("testmonitor.log", "agent-control.log"):
        log_path = root / "logs" / log_name
        if not log_path.is_file():
            continue
        for match in MAC_BYTES_PATTERN.finditer(log_path.read_bytes()):
            log_hardware_ids.add(match.group(0).decode("ascii").upper())
    if hardware_id and log_hardware_ids and log_hardware_ids != {hardware_id}:
        raise errors.ExecutorError("HARDWARE_ID_MISMATCH", "Serialized and Agent-log hardware IDs differ", sorted(log_hardware_ids))
    wrapper_files = sorted(path.name for path in (root / "wrapper").iterdir() if path.is_file())
    toolchain_info_dir = root / "toolchaininfo"
    toolchain_info_files = (
        sorted(path.name for path in toolchain_info_dir.iterdir() if path.is_file())
        if toolchain_info_dir.is_dir()
        else []
    )
    provisioning_state = "PRESENT_UNVERIFIED" if wrapper_files and toolchain_info_files else "UNPROVISIONED"
    descriptor = fileio.add_integrity(
        {
            "schema": AGENT_SCHEMA,
            "agentRoot": str(root),
            "agentRootSource": root_source,
            "mode": mode,
            "launcher": str(launcher),
            "java": str(java),
            "controllerJar": str(controller_jar),
            "agentJar": str(agent_jar),
            "agentJvmOptions": list(tls_transport["jvmOptions"]) if tls_transport else [],
            "tlsTransport": tls_transport or {
                "mode": (
                    "DEFAULT_JAVA_TLS_VERIFICATION"
                    if urllib.parse.urlsplit(agent_origin).scheme.casefold() == "https"
                    else "PLAINTEXT_HTTP"
                ),
                "serverOrigin": agent_origin,
            },
            "hardwareIdFile": str(hardware_file),
            "hardwareId": hardware_id,
            "logHardwareIds": sorted(log_hardware_ids),
            "toolchainProvisioning": {
                "state": provisioning_state,
                "wrapperDirectory": str(root / "wrapper"),
                "wrapperFiles": wrapper_files,
                "toolchainInfoDirectory": str(toolchain_info_dir),
                "toolchainInfoDirectoryExists": toolchain_info_dir.is_dir(),
                "toolchainInfoFiles": toolchain_info_files,
            },
            "startCommand": {
                "executable": executable,
                "arguments": arguments,
                "workingDirectory": str(bin_dir),
                "environmentDelta": {
                    "CODESCROLL_TM_INSTALL_PATH": str(root),
                    "CODESCROLL_TM_SERVER_URL": args.server_url,
                    **(product_start["environmentDelta"] if mode == "PROCESS_CONTROLLER" else {}),
                    **({"LD_LIBRARY_PATH": str(root / "lib")} if os.name != "nt" else {}),
                },
            },
        }
    )
    return envelopes.envelope("DISCOVERING_AGENT", "SUCCEEDED", descriptor)


def ports_in_use(ports: Iterable[int]) -> list[int]:
    used: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.settimeout(0.2)
            if stream.connect_ex(("127.0.0.1", int(port))) == 0:
                used.append(int(port))
    return used


def _stop_owned_process(process: subprocess.Popen[Any]) -> str:
    if process.poll() is not None:
        return "NATURAL_EXIT"
    process.terminate()
    try:
        process.wait(timeout=5)
        return "TERMINATED_AFTER_IDENTITY"
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        return "KILLED_AFTER_IDENTITY"


def bootstrap_agent_identity(args: argparse.Namespace) -> dict[str, Any]:
    """Run only the packaged Agent JAR until Java creates its identity file."""
    loaded = fileio.load_json(args.descriptor)
    descriptor = loaded.get("data", loaded)
    if descriptor.get("schema") != AGENT_SCHEMA:
        raise errors.ExecutorError("INVALID_AGENT_DESCRIPTOR", f"Expected {AGENT_SCHEMA}")
    fileio.verify_integrity(descriptor)
    if not args.allow_start:
        raise errors.ExecutorError(
            "AGENT_IDENTITY_BOOTSTRAP_APPROVAL_REQUIRED",
            "Identity bootstrap requires the same explicit permission as Local Agent startup",
        )
    hardware_file = pathutil.canonical(descriptor["hardwareIdFile"], False)
    if descriptor.get("hardwareId"):
        result = envelopes.envelope(
            "BOOTSTRAPPING_AGENT_IDENTITY",
            "SUCCEEDED",
            {
                "hardwareIdFile": str(hardware_file),
                "hardwareId": descriptor["hardwareId"],
                "launched": False,
                "reason": "IDENTITY_ALREADY_PRESENT",
            },
        )
        if args.output:
            fileio.atomic_write_json(pathutil.canonical(args.output, False), result)
        return result
    if hardware_file.exists():
        # Discovery would normally reject this. Never overwrite or repair an
        # unknown serialized identity because that can fork server identity.
        read_hardware_id_file(hardware_file)
        raise errors.ExecutorError(
            "AGENT_IDENTITY_STATE_INCONSISTENT",
            "The descriptor has no hardware ID although its identity file is valid",
            [str(hardware_file)],
        )
    occupied = ports_in_use(args.port)
    if occupied:
        raise errors.ExecutorError(
            "AGENT_IDENTITY_BOOTSTRAP_PORT_CONFLICT",
            "Cannot bootstrap identity while Agent ports are already in use",
            occupied,
        )
    java = pathutil.canonical(descriptor["java"])
    agent_jar = pathutil.canonical(descriptor["agentJar"])
    root = pathutil.canonical(descriptor["agentRoot"])
    log_path = pathutil.canonical(args.log, False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(descriptor.get("startCommand", {}).get("environmentDelta", {}))
    process: subprocess.Popen[Any] | None = None
    hardware_id: str | None = None
    termination = "NOT_STARTED"
    started_at = fileio.utc_now()
    started = time.time()
    with log_path.open("wb") as output:
        try:
            process = subprocess.Popen(
                [str(java), *descriptor.get("agentJvmOptions", []), "-jar", str(agent_jar)],
                cwd=str(agent_jar.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            deadline = time.monotonic() + args.timeout
            identity_signature: tuple[int, int] | None = None
            identity_stable_since: float | None = None
            while time.monotonic() < deadline:
                if hardware_file.is_file():
                    try:
                        candidate_id = read_hardware_id_file(hardware_file)
                        stat = hardware_file.stat()
                        candidate_signature = (stat.st_size, stat.st_mtime_ns)
                        now = time.monotonic()
                        if candidate_signature != identity_signature:
                            identity_signature = candidate_signature
                            identity_stable_since = now
                        elif identity_stable_since is not None and now - identity_stable_since >= 0.2:
                            hardware_id = candidate_id
                            break
                    except errors.ExecutorError as exc:
                        if exc.code != "INVALID_HARDWARE_ID":
                            raise
                        # ObjectOutputStream may still be flushing the file.
                if process.poll() is not None:
                    break
                time.sleep(0.2)
        finally:
            if process is not None:
                termination = _stop_owned_process(process)
    if hardware_id is None and hardware_file.is_file():
        hardware_id = read_hardware_id_file(hardware_file)
    if hardware_id is None:
        exit_code = process.poll() if process is not None else None
        raise errors.ExecutorError(
            "AGENT_IDENTITY_BOOTSTRAP_FAILED",
            "The packaged Local Agent did not create .cstm_HardwareAddress",
            [str(log_path), f"exitCode={exit_code}"],
        )
    result = envelopes.envelope(
        "BOOTSTRAPPING_AGENT_IDENTITY",
        "SUCCEEDED",
        {
            "agentRoot": str(root),
            "hardwareIdFile": str(hardware_file),
            "hardwareId": hardware_id,
            "launcherPid": process.pid if process is not None else None,
            "launched": True,
            "termination": termination,
            "startedAt": started_at,
            "durationSeconds": round(time.time() - started, 3),
            "log": str(log_path),
            "identitySource": "LOCAL_AGENT_JAVA_SERIALIZATION",
        },
    )
    if args.output:
        fileio.atomic_write_json(pathutil.canonical(args.output, False), result)
    return result


def start_agent(args: argparse.Namespace) -> dict[str, Any]:
    loaded = fileio.load_json(args.descriptor)
    descriptor = loaded.get("data", loaded)
    if descriptor.get("schema") != AGENT_SCHEMA:
        raise errors.ExecutorError("INVALID_AGENT_DESCRIPTOR", f"Expected {AGENT_SCHEMA}")
    fileio.verify_integrity(descriptor)
    if not args.allow_start:
        raise errors.ExecutorError("AGENT_START_APPROVAL_REQUIRED", "Use --allow-start after the start action is in scope")
    if descriptor["mode"] == "PROCESS_CONTROLLER":
        occupied = ports_in_use(args.port)
        if occupied:
            raise errors.ExecutorError("AGENT_ALREADY_RUNNING_OR_PORT_CONFLICT", "Agent ports are already in use", occupied)
    spec = descriptor["startCommand"]
    executable = pathutil.canonical(spec["executable"])
    cwd = pathutil.canonical(spec["workingDirectory"])
    root = pathutil.canonical(descriptor["agentRoot"])
    log_path = root / "logs" / "testmonitor.log"
    controller_log_path = root / "logs" / "agent-control.log"
    initial_log_size = log_path.stat().st_size if log_path.is_file() else 0
    initial_controller_log_size = controller_log_path.stat().st_size if controller_log_path.is_file() else 0
    env = os.environ.copy()
    env.update(spec.get("environmentDelta", {}))
    visible_windows_controller = os.name == "nt" and descriptor["mode"] == "PROCESS_CONTROLLER"
    # The scripts do not expose JVM arguments. Keep TLS options in this Agent
    # process tree only; do not add Java options to cmd/bash arguments.
    jvm_options = descriptor.get("agentJvmOptions") or []
    if jvm_options:
        if any('"' in option or "\n" in option or "\r" in option for option in jvm_options):
            raise errors.ExecutorError("INVALID_AGENT_JVM_OPTION", "Agent JVM options cannot contain quotes or newlines")
        env["JAVA_TOOL_OPTIONS"] = " ".join(
            [env.get("JAVA_TOOL_OPTIONS", ""), *['"' + option + '"' for option in jvm_options]]
        ).strip()
    command = [str(executable), *spec.get("arguments", [])]
    if visible_windows_controller:
        # Popen's list quoting follows the C runtime, not cmd.exe /s /c.
        # Expand the quoted launcher variable in cmd, without CALL/re-expansion.
        command = subprocess.list2cmdline([str(executable)]) + " " + " ".join(spec.get("arguments", []))
    descriptor_path = Path(args.descriptor)
    if visible_windows_controller and env.get("COVER_AGENT_START_SCRIPT"):
        env["COVER_AGENT_START_SCRIPT"] = str(_prepare_windows_agent_launcher(
            Path(env["COVER_AGENT_START_SCRIPT"]), descriptor_path.parent))
    stdout_path = descriptor_path.with_name("agent-start.stdout.log")
    stderr_path = descriptor_path.with_name("agent-start.stderr.log")
    diagnostic_offsets = {p: p.stat().st_size if p.is_file() else 0 for p in (stdout_path, stderr_path)}
    # Keep the Windows console lifecycle, but retain startup failures in logs.
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=None if visible_windows_controller else subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if visible_windows_controller else 0,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()
    deadline = time.time() + args.timeout
    ready = False
    while time.time() < deadline:
        startup_diagnostics = b""
        # The packaged Windows controller can detach after it has started the
        # Java controller process.  Its successful exit is therefore not a
        # readiness failure; retain the log-based readiness check.  A nonzero
        # exit still means the launcher itself failed.
        if process.poll() is not None and process.returncode != 0:
            raise errors.ExecutorError("AGENT_START_FAILED", f"Agent launcher exited with {process.returncode}",
                                [str(stdout_path), str(stderr_path), str(root / 'logs')])
        if log_path.is_file():
            content = log_path.read_bytes()
            new_content = content[initial_log_size:] if len(content) >= initial_log_size else content
            startup_diagnostics += new_content
            if b"AgentReportService Initial Success" in new_content:
                ready = True
                break
        if controller_log_path.is_file():
            controller_content = controller_log_path.read_bytes()
            new_controller_content = (
                controller_content[initial_controller_log_size:]
                if len(controller_content) >= initial_controller_log_size
                else controller_content
            )
            startup_diagnostics += new_controller_content
            if b"RemoteConfiguration not found" in new_controller_content:
                raise errors.ExecutorError(
                    "AGENT_START_FAILED",
                    "The COVER server has no RemoteConfiguration for the local Agent hardware identity",
                    [str(controller_log_path)],
                )
        for diagnostic_path in (stdout_path, stderr_path):
            if diagnostic_path.is_file():
                startup_diagnostics += diagnostic_path.read_bytes()[diagnostic_offsets[diagnostic_path]:]
        if b"hostname in certificate didn't match" in startup_diagnostics:
            raise errors.ExecutorError(
                "AGENT_TLS_HOSTNAME_MISMATCH",
                "The Local Agent rejected the HTTPS server because its certificate does not cover the configured host",
                [str(stdout_path), str(stderr_path)],
            )
        if b".cstm_lock" in startup_diagnostics and b"FileNotFoundException" in startup_diagnostics:
            raise errors.ExecutorError(
                "AGENT_START_FAILED",
                "The Local Agent controller cannot open .cstm_lock; run the Agent with permission to write its installation directory",
                [str(stdout_path), str(stderr_path)],
            )
        time.sleep(1)
    if not ready:
        raise errors.ExecutorError("AGENT_START_TIMEOUT", "Agent reporting did not become ready", [str(log_path), str(stdout_path), str(stderr_path)])
    return envelopes.envelope("STARTING_AGENT", "SUCCEEDED", {"launcherPid": process.pid, "localReportingReady": True, "serverOnlineVerified": False})
