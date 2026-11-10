"""The MCP tool catalog: what COVERA exposes, and how each tool maps onto a
skill entry script.

Unlike a single-CLI plugin, COVERA has three independent skill entry points,
each with its own argument grammar:

- measure   -> skills/cover-ee-coverage-measure/scripts/measure.py
- recovery  -> skills/cover-toolchain-recovery/scripts/cover_recovery.py <subcommand>
- build     -> skills/cover-ee-coverage-build/scripts/cover_auth.py -- <runner args>

So each tool spec names an ``entry`` (one of those three) and, for recovery, a
``subcommand``. ``to_argv`` turns the validated arguments into the flag list;
``server.py`` prepends the interpreter, the entry script, and (for recovery)
the subcommand, and for build wraps the flags after ``--`` and appends an
``--output`` capture file.

The procedure a skill follows -- when to plan vs execute, how to resume, what
to confirm with the user -- lives in the SKILL.md / reference docs, served back
through ``covera_get_guide`` (see guides.py) so a skill-less MCP client has the
same guidance.

tests/test_mcp.py checks that every recovery subcommand and every documented
build/measure flag is represented here.
"""

# Subprocess time limits (seconds). Exceeding one kills the whole process tree.
DEFAULT_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 600        # server issue download / collection
SERVER_PA_TIMEOUT = 1800      # a server-side PA verification run
BUILD_PLAN_TIMEOUT = 1800     # discovery + planning, no build executed
BUILD_EXECUTE_TIMEOUT = 7200  # original + instrumented build can be long

GUIDE_TOPICS = [
    "build", "build-workflow", "build-discovery", "build-gcc",
    "build-visual-studio", "build-stm32cubeide", "build-java", "build-csharp",
    "build-original-commands", "build-decision-contract",
    "build-environment-survey", "build-security-api", "build-reporting",
    "build-architecture", "build-performance",
    "measure",
    "recovery", "recovery-local-workflow", "recovery-server-api",
    "recovery-server-execution", "recovery-server-publication",
    "recovery-artifact-discovery", "recovery-pa-options",
    "recovery-analysis-guide", "recovery-guide-sources",
]


def _ann(read_only=False, destructive=False, idempotent=False, open_world=False):
    """MCP tool annotations. For a read-only tool the write hints are omitted."""
    if read_only:
        return {"readOnlyHint": True, "openWorldHint": open_world}
    return {"readOnlyHint": False, "destructiveHint": destructive,
            "idempotentHint": idempotent, "openWorldHint": open_world}


# --- reusable parameter fragments ----------------------------------------

def _str(desc, flag, *, required=False, abspath=False, isdir=False, enum=None):
    p = {"type": "string", "description": desc, "flag": flag, "kind": "value"}
    if required:
        p["required"] = True
    if abspath:
        p["abspath"] = True
    if isdir:
        p["isdir"] = True
    if enum:
        p["enum"] = enum
    return p


def _bool(desc, flag):
    return {"type": "boolean", "description": desc, "flag": flag, "kind": "flag"}


def _int(desc, flag):
    return {"type": "integer", "description": desc, "flag": flag, "kind": "value"}


def _list(desc, flag, *, abspath=False):
    p = {"type": "array", "items": {"type": "string"}, "description": desc,
         "flag": flag, "kind": "repeat"}
    if abspath:
        p["abspath"] = True
    return p


# Discovery options shared by the recovery subcommands.
_RECOVERY_DISCOVERY = {
    "projectRoot": _str("Absolute path of the build working directory where the "
                        "analysis-error command ran.", "--project-root",
                        abspath=True, isdir=True),
    "agentRoot": _str("Absolute path of the COVER EE Local Agent install folder, "
                      "if it should override the environment.", "--agent-root",
                      abspath=True),
    "serverRoot": _str("Absolute path of a local COVER server install folder or "
                       "catalina.home.", "--server-root", abspath=True),
    "hardwareFile": _str("Absolute path of the build account's HardwareID file.",
                         "--hardware-file", abspath=True),
    "hardwareId": _str("Build Agent HardwareID; omit to read it from the build "
                       "account file.", "--hardware-id"),
    "searchRoots": _list("Extra folders or ZIP files to search; repeatable.",
                         "--search-root", abspath=True),
    "server": _str("COVER server root URL for this recovery.", "--server"),
    "verifyTls": _bool("Verify the HTTPS certificate and hostname. Omitted, TLS "
                       "verification is skipped (the default on internal COVER "
                       "servers with self-signed certificates).", "--verify-tls"),
}


TOOLS = [
    # -- environment / guidance (no skill subprocess) --------------------
    {
        "name": "covera_doctor",
        "entry": "doctor",
        "title": "Check the COVERA environment",
        "description": (
            "Report whether this MCP server can drive COVER: the Python it runs "
            "on, whether CODESCROLL_TM_INSTALL_PATH points at a COVER EE Local "
            "Agent, whether cached Open API credentials exist, whether "
            "COVER_ACCESS_KEY is set, and which skill entry scripts are present. "
            "Needs no project. Run this first when other tools fail with setup "
            "errors."),
        "params": {},
        "annotations": _ann(read_only=True),
    },
    {
        "name": "covera_get_guide",
        "entry": "guide",
        "builtin": "guide",
        "title": "Read a COVERA skill procedure",
        "description": (
            "Return the procedure a COVERA skill follows, so a client without "
            "the skills can work the same way. Topics cover the coverage build "
            "flow, module measurement, and toolchain recovery, plus their "
            "reference documents. Read the matching topic before driving a flow."),
        "params": {
            "topic": {
                "type": "string",
                "description": "Which procedure to read.",
                "enum": GUIDE_TOPICS,
                "required": True,
            },
        },
        "annotations": _ann(read_only=True),
    },

    # -- measure (read-only coverage lookup) -----------------------------
    {
        "name": "covera_coverage_measure",
        "entry": "measure",
        "title": "Look up module coverage",
        "description": (
            "Read a module's coverage results from the COVER server: the module "
            "summary, and optionally per-file and per-function detail. Read-only; "
            "runs nothing and changes nothing. Give exactly one of a module name "
            "(a filename or path suffix to search for) or a 24-hex module id. The "
            "server URL and Access Key come from the Local Agent's cached Open "
            "API credentials; set CODESCROLL_TM_INSTALL_PATH if they are not "
            "found (see covera_doctor)."),
        "params": {
            "moduleName": _str("Full filename or path suffix of the module to "
                               "find. The server may return several candidates.",
                               "--module-name"),
            "moduleId": _str("24-hex build module id (not a symbolicmoduleid "
                             "URL).", "--module-id"),
            "details": _bool("Include per-file and per-function coverage, not "
                             "just the module summary.", "--details"),
            "limit": _int("Rows per detail page (1-2000, default 100).",
                          "--limit"),
            "maxPages": _int("Maximum detail pages to read (default 1000).",
                             "--max-pages"),
            "serverUrl": _str("COVER Open API main web server URL; defaults to "
                              "the cached one.", "--server-url"),
            "verifyTls": _bool("Verify the HTTPS certificate and hostname "
                               "(skipped by default).", "--verify-tls"),
        },
        "one_of": [["moduleName", "moduleId"]],
        "annotations": _ann(read_only=True, open_world=True),
        "timeout": DEFAULT_TIMEOUT,
    },

    # -- coverage build (plan, then optionally execute) ------------------
    {
        "name": "covera_coverage_build",
        "entry": "build",
        "title": "Run a COVER coverage build",
        "description": (
            "Drive a COVER EE coverage build for a C/C++, C#, or Java project: "
            "identify the original build and real tools, plan the instrumented "
            "build keeping the original output paths, and -- when execute is true "
            "-- run it and keep the successful setup for later builds. Give "
            "projectDir to start a new build or jobContext to resume one "
            "(exactly one). execute runs the real builds and registers the "
            "coverage module on the COVER server, so it changes the project's "
            "build setup and server state: read covera_get_guide topic=build "
            "first, explain what it does, and get the user's approval before "
            "calling with execute. With execute omitted it only plans and writes "
            "job state. The result is a workflow envelope; a WAITING_* status "
            "means resume with its jobContext after supplying what it asks for. "
            "The Open API Access Key is read from the Local Agent's cached "
            "credentials or COVER_ACCESS_KEY; this server never prompts for it."),
        "params": {
            "projectDir": _str("Absolute path of the project to build (start a "
                               "new workflow).", "--project-root",
                               abspath=True, isdir=True),
            "jobContext": _str("Absolute path of an existing job.json to resume.",
                               "--job-context", abspath=True),
            "openApiServerUrl": _str("COVER Open API main web server URL (e.g. "
                                     "https://cover.example.com:4009); required "
                                     "for a new build unless already cached.",
                                     "--open-api-server-url"),
            "execute": _bool("Run the validated original and coverage builds "
                             "after planning. Without it, plan only.", "--execute"),
            "failFast": _bool("Stop on the first coverage build failure; do not "
                              "run the project-file fallback.", "--fail-fast"),
            "serverUrl": _str("Local Agent remote-service URL; defaults to "
                              "CODESCROLL_TM_SERVER_URL.", "--server-url"),
            "verifyTls": _bool("Verify the HTTPS certificate for a new COVER job "
                               "(skipped by default).", "--verify-tls"),
            "noEnvironmentProfile": _bool("Do not read a previous job's "
                                          "environment profile.",
                                          "--no-environment-profile"),
            "jobsRoot": _str("Absolute path of the jobs root directory.",
                             "--jobs-root", abspath=True),
            "decision": _str("Absolute path of an integrity-protected decision "
                             "file produced by this runner.", "--decision",
                             abspath=True),
            "parallel": _int("Build parallelism (default: host CPU count).",
                             "--parallel"),
            "targets": _list("Native GCC build target; repeatable.", "--target"),
            "artifacts": _list("Expected build artifact; repeatable.",
                               "--artifact"),
            "gccBuildTool": _str("Explicit cmake or make executable.",
                                 "--gcc-build-tool"),
            "gccBuildDirectory": _str("GCC build directory.",
                                      "--gcc-build-directory"),
            "gccGenerator": _str("CMake generator.", "--gcc-generator"),
            "gccConfiguration": _str("Build configuration.", "--gcc-configuration"),
            "gccConfigureArgs": _list("Extra configure argument; repeatable.",
                                      "--gcc-configure-arg"),
            "gccCleanTarget": _str("Clean target name (default: clean).",
                                   "--gcc-clean-target"),
            "gccCoverConfig": _str("COVER config filename under <agent-root>/conf "
                                   "(default: gcc.config).", "--gcc-cover-config"),
            "stm32cubeideExecutable": _str("Explicit stm32cubeidec.exe / "
                                           "stm32cubeide for headless builds.",
                                           "--stm32cubeide-executable"),
            "vsRealTools": _list("Fallback cl.exe/link.exe/lib.exe as TOOL=PATH; "
                                 "repeatable.", "--vs-real-tool"),
        },
        "one_of": [["projectDir", "jobContext"]],
        "annotations": _ann(destructive=True, open_world=True),
        "output_file": True,
    },

    # -- toolchain recovery (subcommands) --------------------------------
    {
        "name": "covera_recovery_discover",
        "entry": "recovery",
        "subcommand": "discover",
        "title": "Survey a recovery (read-only)",
        "description": (
            "Read the build environment, COVER server settings, and available "
            "analysis-error material without changing anything, and report what "
            "is missing to collect it. Read-only."),
        "params": dict({
            "source": _str("Source identifier of the analysis error (required).",
                           "--source", required=True),
        }, **_RECOVERY_DISCOVERY),
        "annotations": _ann(read_only=True, open_world=True),
        "timeout": DEFAULT_TIMEOUT,
    },
    {
        "name": "covera_recovery_start",
        "entry": "recovery",
        "subcommand": "start",
        "title": "Start a recovery case",
        "description": (
            "Copy the analysis-error ZIP/folder (or collect the latest from the "
            "server) into a new run directory and record initial evidence and "
            "state. Does not modify original sources. May download from the "
            "COVER server, which needs COVER_ACCESS_KEY or cached credentials."),
        "params": dict({
            "source": _str("Source identifier of the analysis error (required).",
                           "--source", required=True),
            "runDir": _str("Absolute path of the new run directory to create "
                           "(required).", "--run-dir", required=True, abspath=True),
            "artifacts": _str("Absolute path of error material; omit to auto-find "
                              "from the build context.", "--artifacts",
                              abspath=True),
        }, **_RECOVERY_DISCOVERY),
        "annotations": _ann(open_world=True),
        "inject_key": True,
        "timeout": DOWNLOAD_TIMEOUT,
    },
    {
        "name": "covera_recovery_collect",
        "entry": "recovery",
        "subcommand": "collect",
        "title": "Collect error material from the server",
        "description": (
            "Download the latest PA analysis-error material over the Open API "
            "into a new ZIP and start the case. Needs COVER_ACCESS_KEY or cached "
            "credentials."),
        "params": dict({
            "source": _str("Source identifier of the analysis error (required).",
                           "--source", required=True),
            "archive": _str("Absolute path for the new error ZIP (required).",
                            "--archive", required=True, abspath=True),
            "runDir": _str("Absolute path of the new run directory (required).",
                           "--run-dir", required=True, abspath=True),
        }, **_RECOVERY_DISCOVERY),
        "annotations": _ann(open_world=True),
        "inject_key": True,
        "timeout": DOWNLOAD_TIMEOUT,
    },
    {
        "name": "covera_recovery_verify",
        "entry": "recovery",
        "subcommand": "verify",
        "title": "Verify a recovery candidate",
        "description": (
            "Run a server-side PA verification of an evidence-backed candidate "
            "for a case. Runs analysis on the COVER server but makes no permanent "
            "server change. Needs COVER_ACCESS_KEY or cached credentials."),
        "params": {
            "case": _str("Absolute path of the case directory (required).",
                         "--case", required=True, abspath=True),
            "candidate": _str("Absolute path of the candidate file (required).",
                              "--candidate", required=True, abspath=True),
            "timeout": _int("Status-poll wait in seconds (1-60); the server run "
                            "continues regardless.", "--timeout"),
            "platform": _str("Build PC OS; omit to let the server decide.",
                             "--platform", enum=["win", "linux"]),
            "server": _str("COVER server root URL; defaults to the case's "
                           "collection server.", "--server"),
            "verifyTls": _bool("Verify the HTTPS certificate (skipped by "
                               "default).", "--verify-tls"),
        },
        "annotations": _ann(open_world=True),
        "inject_key": True,
        "timeout": SERVER_PA_TIMEOUT,
    },
    {
        "name": "covera_recovery_inspect",
        "entry": "recovery",
        "subcommand": "inspect",
        "title": "Inspect a recovery case",
        "description": "Read the recorded run history of a case. Local and "
                       "read-only.",
        "params": {
            "case": _str("Absolute path of the case directory (required).",
                         "--case", required=True, abspath=True),
        },
        "annotations": _ann(read_only=True),
        "timeout": DEFAULT_TIMEOUT,
    },
    {
        "name": "covera_recovery_publish_plan",
        "entry": "recovery",
        "subcommand": "publish-plan",
        "title": "Plan a server change",
        "description": (
            "Produce a server change plan for a verified case; reads the COVER "
            "server only. Returns a plan_digest to pass to covera_recovery_"
            "publish. Needs COVER_ACCESS_KEY or cached credentials."),
        "params": {
            "server": _str("COVER server root URL (required).", "--server",
                           required=True),
            "case": _str("Absolute path of the case directory (required).",
                         "--case", required=True, abspath=True),
            "hardwareId": _str("Build Agent HardwareID (required).",
                               "--hardware-id", required=True),
            "wrapper": _str("Wrapper name to connect (required).", "--wrapper",
                            required=True),
            "verifyTls": _bool("Verify the HTTPS certificate (skipped by "
                               "default).", "--verify-tls"),
        },
        "annotations": _ann(open_world=True),
        "inject_key": True,
        "timeout": DEFAULT_TIMEOUT,
    },
    {
        "name": "covera_recovery_publish",
        "entry": "recovery",
        "subcommand": "publish",
        "title": "Apply a server change",
        "description": (
            "Apply a verified plan to the COVER server and connect the wrapper. "
            "This changes the shared COVER server, so confirm with the user "
            "first. Pass the plan_digest from covera_recovery_publish_plan. Needs "
            "COVER_ACCESS_KEY or cached credentials."),
        "params": {
            "server": _str("COVER server root URL (required).", "--server",
                           required=True),
            "case": _str("Absolute path of the case directory (required).",
                         "--case", required=True, abspath=True),
            "planDigest": _str("plan_digest from publish-plan (required).",
                               "--plan-digest", required=True),
            "verifyTls": _bool("Verify the HTTPS certificate (skipped by "
                               "default).", "--verify-tls"),
        },
        "annotations": _ann(destructive=True, open_world=True),
        "inject_key": True,
        "timeout": DOWNLOAD_TIMEOUT,
    },
]


_BY_NAME = {t["name"]: t for t in TOOLS}


def by_name(name):
    return _BY_NAME.get(name)


def _schema(spec):
    """JSON Schema for a tool's inputs, built from its params."""
    properties = {}
    required = []
    for name, p in spec["params"].items():
        node = {"type": p["type"], "description": p["description"]}
        if p.get("enum"):
            node["enum"] = p["enum"]
        if p["type"] == "array":
            node["items"] = p.get("items", {"type": "string"})
        properties[name] = node
        if p.get("required"):
            required.append(name)
    schema = {"type": "object", "properties": properties,
              "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def tool_listing(spec):
    """A tools/list entry for one spec."""
    return {
        "name": spec["name"],
        "title": spec["title"],
        "description": spec["description"],
        "inputSchema": _schema(spec),
        "annotations": dict(spec["annotations"], title=spec["title"]),
    }


def to_argv(spec, arguments):
    """Turn validated arguments into the flag list (no entry path, no
    subcommand, no --output: server.py composes those)."""
    argv = []
    for name, p in spec["params"].items():
        if name not in arguments:
            continue
        value = arguments[name]
        kind = p.get("kind", "value")
        flag = p.get("flag")
        if flag is None:
            continue
        if kind == "flag":
            if value:
                argv.append(flag)
        elif kind == "repeat":
            for item in value:
                argv.extend([flag, str(item)])
        else:  # value
            argv.extend([flag, str(value)])
    return argv


def timeout_for(spec, arguments):
    if spec.get("entry") == "build":
        return BUILD_EXECUTE_TIMEOUT if arguments.get("execute") \
            else BUILD_PLAN_TIMEOUT
    return spec.get("timeout", DEFAULT_TIMEOUT)


def validate(spec, arguments, isabs, isdir):
    """Return an error string, or None when the arguments are acceptable.

    Input errors come back as a tool result (not a JSON-RPC error) so the model
    can read the message and retry.
    """
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    params = spec["params"]
    for key in arguments:
        if key not in params:
            return "unknown argument: %s" % key

    for group in spec.get("one_of", []):
        present = [g for g in group if g in arguments]
        if len(present) != 1:
            return "provide exactly one of: %s" % ", ".join(group)

    for name, p in params.items():
        if p.get("required") and name not in arguments:
            return "missing required argument: %s" % name
        if name not in arguments:
            continue
        value = arguments[name]
        kind = p.get("kind", "value")
        if kind == "flag":
            if not isinstance(value, bool):
                return "%s must be true or false" % name
            continue
        if kind == "repeat":
            if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value):
                return "%s must be an array of strings" % name
            items = value
        else:
            if p["type"] == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    return "%s must be an integer" % name
                items = []
            else:
                if not isinstance(value, str):
                    return "%s must be a string" % name
                if p.get("enum") and value not in p["enum"]:
                    return "%s must be one of: %s" % (name, ", ".join(p["enum"]))
                items = [value]
        if p.get("abspath"):
            for item in items:
                if not isabs(item):
                    return "%s must be an absolute path: %s" % (name, item)
        if p.get("isdir"):
            for item in items:
                if not isdir(item):
                    return "%s is not an existing directory: %s" % (name, item)
    return None
