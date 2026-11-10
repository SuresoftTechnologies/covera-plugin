"""stdio MCP server -- exposes the COVERA skill scripts as tools.

Each tools/call runs one short-lived subprocess: measure.py, cover_recovery.py
<subcommand>, or cover_auth.py -- <cover_workflow_runner args>. The handlers are
never called in-process, for the same reasons a wrapper is used elsewhere:

- A skill script (or a build tool it spawns) may write to its own stdout; here
  that is a pipe we read and discard, so it can never corrupt the MCP stream.
- Module globals and the heavy cover_build import graph stay out of this
  process, so the server has no third-party dependency and starts fast.
- Each call goes through the exact code path the skills use.

The one thing read in-process is the cached Open API server URL (to decide
whether to bridge COVER_ACCESS_KEY into a recovery subprocess). cover_build is
never imported here, because importing it can reconfigure stdout.

Written for Python 3.10+ (the skill runtimes need it).
"""
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import guides
from . import tools as catalog

SERVER_INFO = {"name": "covera-mcp", "title": "COVERA", "version": "0.1.0"}
# Newest first. An unknown requested version is answered with the newest.
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MIN_PYTHON = (3, 11)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

# entry key -> (skill name, entry script filename under the skill's scripts/)
ENTRY_SCRIPTS = {
    "measure": ("cover-ee-coverage-measure", "measure.py"),
    "recovery": ("cover-toolchain-recovery", "cover_recovery.py"),
    "build": ("cover-ee-coverage-build", "cover_auth.py"),
}

INSTRUCTIONS = """\
COVERA drives Suresoft COVER EE on the local machine: coverage builds, module \
coverage lookups, and C/C++ toolchain-error recovery.
- Coverage build: read covera_get_guide topic=build first. Plan with \
covera_coverage_build (no execute); explain what execute does -- it changes the \
project build setup and registers the module on the shared COVER server -- and \
get the user's approval before calling with execute. A WAITING_* status means \
resume with its jobContext, not a fresh start.
- Measurement is read-only; give exactly one of a module name or a 24-hex id.
- Recovery: covera_recovery_publish changes the shared COVER server; confirm \
first. Do not modify original sources.
- The Open API Access Key is read from the Local Agent's cached credentials or \
COVER_ACCESS_KEY. Never ask for, print, or store it.
- On setup errors run covera_doctor.
"""

# How often (seconds) to send a progress notification on a long call, when the
# client supplied a progressToken.
PROGRESS_INTERVAL = 15

COVER_KEY_ENV = "COVER_ACCESS_KEY"
# The COVER EE Local Agent install path. Set in the environment when the Local
# Agent is installed; the server reads it from the inherited environment and
# never overrides it.
CLI_INSTALL_ENV = "CODESCROLL_TM_INSTALL_PATH"


def python_ok():
    return sys.version_info[:2] >= MIN_PYTHON


def _python_info():
    return {"executable": sys.executable,
            "version": "%d.%d.%d" % sys.version_info[:3],
            "supported": python_ok(),
            "required": "%d.%d+" % MIN_PYTHON}


def python_too_old():
    return {
        "ok": False, "code": "PYTHON_TOO_OLD",
        "message": ("COVERA needs Python %d.%d or later; this MCP server runs "
                    "on %s (%s)." % (MIN_PYTHON[0], MIN_PYTHON[1],
                                     _python_info()["version"], sys.executable)),
        "hint": ("Install Python %d.%d+ or point COVERA_PYTHON at one, then "
                 "restart the MCP client.") % MIN_PYTHON,
        "mcpServer": dict(SERVER_INFO, python=_python_info()),
    }


# --------------------------------------------------------------------------
# entry scripts and credentials
# --------------------------------------------------------------------------

def entry_path(entry):
    """Absolute path of a skill entry script, or None when its skill is absent."""
    skill_id, filename = ENTRY_SCRIPTS[entry]
    skill_dir = guides._skill_dir(skill_id)
    if skill_dir is None:
        return None
    path = skill_dir / "scripts" / filename
    return path if path.is_file() else None


def _cached_credentials(env):
    """The cached Open API {url, hasKey} from the Local Agent install, or None.

    Reads only with the standard library; cover_build is never imported here.
    The Access Key itself is never returned or logged -- only whether one is
    present and the (non-secret) server URL.
    """
    install = (env.get(CLI_INSTALL_ENV) or "").strip()
    if not install:
        return None
    path = Path(install) / "agent_kit" / "open-api-credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("openApiServerUrl")
    key = data.get("accessKey")
    if isinstance(url, str) and url.strip() and isinstance(key, str) and key.strip():
        return {"url": url.strip(), "_key": key, "path": str(path)}
    return None


def _origin_identity(url):
    try:
        parsed = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    return (parsed.scheme or "https", host, parsed.port or 443)


def resolve_cover_key(env, server_url):
    """The COVER Access Key to hand a server-talking recovery subprocess, or None.

    COVER_ACCESS_KEY in the environment wins for any server (matching the
    product's own resolution). Otherwise the cached key is used only when the
    requested server matches the cached Open API server -- a mismatch means the
    key is not handed over. The key is used only to set the child's environment
    and is never logged or returned to the client.
    """
    existing = env.get(COVER_KEY_ENV)
    if existing:
        return existing
    if not server_url:
        return None
    cached = _cached_credentials(env)
    if cached is None:
        return None
    want = _origin_identity(server_url)
    have = _origin_identity(cached["url"])
    if want and have and want == have:
        return cached["_key"]
    return None


# --------------------------------------------------------------------------
# subprocess
# --------------------------------------------------------------------------

def _extract_json(text):
    """The last top-level JSON object in text. The scripts print one final JSON
    object; the build runner also prints [COVER] progress lines, so scan."""
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    found = None
    idx = text.find("{")
    while idx >= 0:
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except ValueError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        idx = text.find("{", idx + end)
    return found


def _popen_kwargs():
    if sys.platform == "win32":
        flags = 0x08000000 | subprocess.CREATE_NEW_PROCESS_GROUP  # CREATE_NO_WINDOW
        return {"creationflags": flags}
    return {"start_new_session": True}


def _kill_tree(proc):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30,
                           creationflags=0x08000000)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(cmd, cwd=None, timeout=catalog.DEFAULT_TIMEOUT, on_tick=None,
                env_extra=None, label="command"):
    """Run cmd (a full argv) and return (stdout_text, returncode, error).

    error is set only when the process could not be started or did not finish;
    otherwise stdout_text and returncode are set.
    """
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if env_extra:
        env.update(env_extra)
    try:
        # stdin is DEVNULL: a child must not read the MCP stream, and a stray
        # getpass() in the build auth wrapper then reads EOF (empty) instead of
        # blocking, so a missing credential becomes a clean WAITING_* envelope.
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **_popen_kwargs())
    except OSError as exc:
        return None, None, "could not start %s (%s)" % (label, exc)
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        remaining = timeout - elapsed
        if remaining <= 0:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=30)
            except (subprocess.SubprocessError, OSError):
                pass
            return None, None, ("%s did not finish within %d seconds and was "
                                "stopped." % (label, timeout))
        wait = min(remaining, PROGRESS_INTERVAL) if on_tick else remaining
        try:
            out, err = proc.communicate(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            if on_tick and time.monotonic() - started < timeout:
                on_tick(int(time.monotonic() - started))
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    return (stdout, stderr), proc.returncode, None


def _no_json(label, streams, returncode):
    stdout, stderr = streams
    detail = (stderr or stdout).strip()[-2000:]
    return ("%s returned no JSON (exit %s).\n%s" % (label, returncode, detail))


# --------------------------------------------------------------------------
# tool dispatch
# --------------------------------------------------------------------------

def guide_result(arguments):
    text, failure = guides.read_guide(arguments.get("topic"))
    if text is None:
        return _text_result(failure, is_error=True)
    return _text_result(text, is_error=False)


def doctor_result(env=None):
    env = dict(os.environ if env is None else env)
    install = (env.get(CLI_INSTALL_ENV) or "").strip()
    cached = _cached_credentials(env)
    scripts = {entry: (entry_path(entry) is not None) for entry in ENTRY_SCRIPTS}
    ready_key = bool(env.get(COVER_KEY_ENV)) or cached is not None
    ok = python_ok() and all(scripts.values())
    payload = {
        "ok": ok,
        "python": _python_info(),
        "installPath": {
            "env": CLI_INSTALL_ENV,
            "set": bool(install),
            "value": install or None,
        },
        "credentials": {
            "cached": cached is not None,
            "path": cached["path"] if cached else None,
            "serverUrl": cached["url"] if cached else None,
            "accessKeyEnvSet": bool(env.get(COVER_KEY_ENV)),
            "serverCallsReady": ready_key,
        },
        "entryScripts": scripts,
        "mcpServer": dict(SERVER_INFO, python=_python_info(),
                          pluginRoot=str(PLUGIN_ROOT)),
    }
    missing = []
    if not python_ok():
        missing.append("Python %d.%d+" % MIN_PYTHON)
    for entry, present in scripts.items():
        if not present:
            missing.append("%s entry script" % entry)
    if not install:
        missing.append("%s (COVER EE Local Agent install path)" % CLI_INSTALL_ENV)
    if not ready_key:
        missing.append("cached Open API credentials or %s "
                       "(needed for server calls)" % COVER_KEY_ENV)
    if missing:
        payload["missing"] = missing
    return _result(payload, is_error=not ok)


def _build_output_path():
    tmp = tempfile.mkdtemp(prefix="covera-mcp-")
    return Path(tmp) / "workflow-state.json", Path(tmp)


def call_tool(spec, arguments, on_tick=None, env=None):
    """Run one tool and return an MCP tools/call result."""
    entry = spec.get("entry")
    if spec.get("builtin") == "guide":
        return guide_result(arguments)
    if entry == "doctor":
        return doctor_result(env)
    if not python_ok():
        payload = python_too_old()
        payload["message"] = "%s is unavailable: %s" % (spec["name"],
                                                        payload["message"])
        return _result(payload, is_error=True)

    script = entry_path(entry)
    if script is None:
        skill_id = ENTRY_SCRIPTS[entry][0]
        return _text_result(
            "%s cannot run: the %s skill is not installed next to this MCP "
            "server." % (spec["name"], skill_id), is_error=True)

    argv = catalog.to_argv(spec, arguments)
    cwd = arguments.get("projectDir")
    timeout = catalog.timeout_for(spec, arguments)
    env_extra = None
    cleanup = None
    label = spec["name"]

    if entry == "measure":
        cmd = [sys.executable, str(script)] + argv
    elif entry == "recovery":
        cmd = [sys.executable, str(script), spec["subcommand"]] + argv
        if spec.get("inject_key"):
            key = resolve_cover_key(os.environ if env is None else env,
                                    arguments.get("server"))
            if key:
                env_extra = {COVER_KEY_ENV: key}
    elif entry == "build":
        out_path, out_dir = _build_output_path()
        cleanup = out_dir
        cmd = [sys.executable, str(script), "--"] + argv + \
            ["--output", str(out_path)]
    else:
        return _text_result("%s: unknown entry %r" % (spec["name"], entry),
                            is_error=True)

    kwargs = {"cwd": cwd, "timeout": timeout, "env_extra": env_extra,
              "label": label}
    if on_tick is not None:
        kwargs["on_tick"] = on_tick
    try:
        streams, returncode, failure = run_command(cmd, **kwargs)
        if failure is not None:
            return _text_result(failure, is_error=True)
        stdout, _stderr = streams
        payload = None
        if entry == "build":
            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
        if payload is None:
            payload = _extract_json(stdout)
        if payload is None:
            return _text_result(_no_json(label, streams, returncode),
                               is_error=True)
        return _result(payload, is_error=_is_error(entry, payload))
    finally:
        if cleanup is not None:
            _rmtree(cleanup)


def _is_error(entry, payload):
    if entry == "measure":
        return payload.get("status") == "FAILED"
    if entry == "recovery":
        return "error" in payload
    if entry == "build":
        return payload.get("status") == "FAILED"
    return False


def _rmtree(path):
    try:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _result(payload, is_error):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _text_result(text, is_error):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------

def negotiate_protocol(requested):
    return requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]


class Server:
    """Reads one message at a time and replies through ``send``.

    Long tools/call requests are handled one at a time by a single worker
    thread, so two builds or recoveries never race on the same workspace. The
    read loop still answers ping / tools/list / cancellation meanwhile.

    Cancellation: a queued call is not started; a call already running is left
    to finish (stopping a build mid-way would leave a half-written workspace),
    only its reply is suppressed.
    """

    def __init__(self, send):
        self._send = send
        self._tools = [catalog.tool_listing(t) for t in catalog.TOOLS]
        self._pending = queue.Queue()
        self._lock = threading.Lock()
        self._outstanding = set()
        self._abandoned = set()
        self._worker = threading.Thread(target=self._work, name="covera-mcp")
        self._worker.start()

    def _result(self, rid, value):
        self._send({"jsonrpc": "2.0", "id": rid, "result": value})

    def _error(self, rid, code, message):
        self._send({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": code, "message": message}})

    def _work(self):
        while True:
            item = self._pending.get()
            if item is None:
                return
            rid, spec, arguments, token = item
            with self._lock:
                skip = rid in self._abandoned
            if not skip:
                on_tick = None
                if token is not None:
                    def on_tick(elapsed, token=token, name=spec["name"]):
                        self._send({"jsonrpc": "2.0",
                                    "method": "notifications/progress",
                                    "params": {"progressToken": token,
                                               "progress": elapsed,
                                               "message": "%s still running "
                                                          "(%ds)" % (name,
                                                                     elapsed)}})
                try:
                    value = call_tool(spec, arguments, on_tick)
                except Exception as exc:  # noqa: BLE001 - worker must not die
                    value = _text_result(
                        "%s failed unexpectedly (%s)." % (spec["name"],
                                                          type(exc).__name__),
                        is_error=True)
                with self._lock:
                    skip = rid in self._abandoned
                if not skip:
                    self._result(rid, value)
            with self._lock:
                self._outstanding.discard(rid)
                self._abandoned.discard(rid)

    def _cancel(self, rid):
        with self._lock:
            if rid in self._outstanding:
                self._abandoned.add(rid)

    def close(self):
        self._pending.put(None)

    def join(self, timeout=None):
        self._worker.join(timeout)

    def handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            req = json.loads(line)
        except ValueError:
            self._error(None, -32700, "Parse error: line is not valid JSON")
            return
        if not isinstance(req, dict):
            self._error(None, -32600, "Invalid Request: expected a JSON object")
            return
        method = req.get("method")
        params = req.get("params")
        if not isinstance(params, dict):
            params = {}
        if "id" not in req:  # notification
            if method == "notifications/cancelled":
                self._cancel(params.get("requestId"))
            return
        rid = req["id"]
        if not isinstance(method, str):
            self._error(rid, -32600, "Invalid Request: 'method' must be a string")
            return

        if method == "initialize":
            self._result(rid, {
                "protocolVersion": negotiate_protocol(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False},
                                 "prompts": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            })
        elif method == "ping":
            self._result(rid, {})
        elif method == "tools/list":
            self._result(rid, {"tools": self._tools})
        elif method == "tools/call":
            self._tools_call(rid, params)
        elif method == "prompts/list":
            self._result(rid, {"prompts": guides.prompt_listing()})
        elif method == "prompts/get":
            arguments = params.get("arguments")
            value, failure = guides.get_prompt(
                params.get("name"), arguments if isinstance(arguments, dict) else {})
            if value is None:
                self._error(rid, -32602, failure)
            else:
                self._result(rid, value)
        else:
            self._error(rid, -32601, "Method not found: %s" % method)

    def _tools_call(self, rid, params):
        name = params.get("name")
        spec = catalog.by_name(name) if isinstance(name, str) else None
        if spec is None:
            self._error(rid, -32602, "Unknown tool: %s" % name)
            return
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        bad = catalog.validate(spec, arguments, os.path.isabs, os.path.isdir)
        if bad is not None:
            self._result(rid, _text_result("%s: %s" % (name, bad), is_error=True))
            return
        if spec.get("builtin") or spec.get("entry") == "doctor":
            # Reading docs / the environment report does not queue behind a long
            # build.
            self._result(rid, call_tool(spec, arguments))
            return
        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        with self._lock:
            self._outstanding.add(rid)
        self._pending.put((rid, spec, arguments, token))


def ensure_utf8_stdio():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main():
    ensure_utf8_stdio()
    lock = threading.Lock()

    def send(message):
        line = json.dumps(message, ensure_ascii=True) + "\n"
        with lock:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError):
                pass

    if not python_ok():
        sys.stderr.write("covera-mcp: Python %s is too old; serving the "
                         "environment report only.\n" % _python_info()["version"])
    server = Server(send)
    try:
        for line in sys.stdin:
            try:
                server.handle_line(line)
            except Exception as exc:  # noqa: BLE001 - one bad message must not
                # tear down the server; the read loop keeps going.
                sys.stderr.write("covera-mcp: dropped a message (%s)\n"
                                 % type(exc).__name__)
    finally:
        server.close()
    return 0
