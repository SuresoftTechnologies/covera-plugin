"""Expose the COVERA skill docs over MCP: the ``covera_get_guide`` tool and the
prompts.

Most of the judgement in a COVERA flow -- build identification, when to plan vs
execute, how to resume a job, what to confirm before changing the project or the
server, how recovery verifies on the server -- lives in the SKILL.md and
reference docs, not in the scripts. A client without the skills reads the same
text here: the docs are read in place (not copied) with one preamble prepended,
so edits to the skill docs flow straight through.

The preamble maps the shell commands in the docs
(``python .../measure.py``, ``cover_recovery.py <subcommand>``,
``cover_workflow_runner.py``) onto the MCP tools, and names what the MCP server
cannot do (run a build shell itself, edit files) so the client arranges it.
"""
import re
from pathlib import Path

# topic -> (skill name from SKILL.md frontmatter, file relative to the skill dir)
TOPICS = {
    "build": ("cover-ee-coverage-build", "SKILL.md"),
    "build-workflow": ("cover-ee-coverage-build", "references/workflow.md"),
    "build-discovery": ("cover-ee-coverage-build",
                        "references/discovery-and-planning.md"),
    "build-gcc": ("cover-ee-coverage-build", "references/gcc-cmake-make.md"),
    "build-visual-studio": ("cover-ee-coverage-build",
                            "references/visual-studio.md"),
    "build-stm32cubeide": ("cover-ee-coverage-build",
                           "references/stm32cubeide.md"),
    "build-java": ("cover-ee-coverage-build", "references/java.md"),
    "build-csharp": ("cover-ee-coverage-build", "references/csharp.md"),
    "build-original-commands": ("cover-ee-coverage-build",
                                "references/original-build-commands.md"),
    "build-decision-contract": ("cover-ee-coverage-build",
                                "references/decision-contract.md"),
    "build-environment-survey": ("cover-ee-coverage-build",
                                 "references/environment-survey.md"),
    "build-security-api": ("cover-ee-coverage-build",
                           "references/security-and-api.md"),
    "build-reporting": ("cover-ee-coverage-build",
                        "references/build-reporting.md"),
    "build-architecture": ("cover-ee-coverage-build",
                           "references/architecture.md"),
    "build-performance": ("cover-ee-coverage-build", "references/performance.md"),
    "measure": ("cover-ee-coverage-measure", "SKILL.md"),
    "recovery": ("cover-toolchain-recovery", "SKILL.md"),
    "recovery-local-workflow": ("cover-toolchain-recovery",
                                "references/local-workflow.md"),
    "recovery-server-api": ("cover-toolchain-recovery",
                            "references/server-api.md"),
    "recovery-server-execution": ("cover-toolchain-recovery",
                                  "references/server-execution.md"),
    "recovery-server-publication": ("cover-toolchain-recovery",
                                    "references/server-publication.md"),
    "recovery-artifact-discovery": ("cover-toolchain-recovery",
                                    "references/artifact-discovery.md"),
    "recovery-pa-options": ("cover-toolchain-recovery", "references/pa-options.md"),
    "recovery-analysis-guide": ("cover-toolchain-recovery",
                                "references/analysis-guide.md"),
    "recovery-guide-sources": ("cover-toolchain-recovery",
                               "references/guide-sources.md"),
}

# Which MCP tool replaces each skill's shell entry point, for the preamble.
_ENTRY_TOOLS = [
    ("cover_workflow_runner.py / cover_auth.py (coverage build)",
     "covera_coverage_build"),
    ("measure.py (module coverage lookup)", "covera_coverage_measure"),
    ("cover_recovery.py discover", "covera_recovery_discover"),
    ("cover_recovery.py start", "covera_recovery_start"),
    ("cover_recovery.py collect", "covera_recovery_collect"),
    ("cover_recovery.py verify", "covera_recovery_verify"),
    ("cover_recovery.py inspect", "covera_recovery_inspect"),
    ("cover_recovery.py publish-plan", "covera_recovery_publish_plan"),
    ("cover_recovery.py publish", "covera_recovery_publish"),
]

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)
_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.M)


def skills_root():
    """``<plugin root>/skills``. guides.py is at scripts/covera_mcp/guides.py."""
    return Path(__file__).resolve().parents[2] / "skills"


def _skill_dir(skill_id):
    """The skill directory. A vendor adapter may rename the folder, so fall back
    to matching the SKILL.md frontmatter name."""
    root = skills_root()
    direct = root / skill_id
    if (direct / "SKILL.md").is_file():
        return direct
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            head = skill_md.read_text(encoding="utf-8")[:2000]
        except OSError:
            continue
        match = _NAME.search(head)
        if match and match.group(1).strip("'\"") == skill_id:
            return skill_md.parent
    return None


def _tool_table():
    rows = ["| Skill shell entry | MCP tool |", "|---|---|"]
    for entry, tool in _ENTRY_TOOLS:
        rows.append("| `%s` | `%s` |" % (entry, tool))
    return "\n".join(rows)


def _preamble(topic):
    header = _tool_table()
    return (
        "> **Reading this through MCP** (covera_get_guide topic=%s)\n>\n"
        "> Below is the procedure a COVERA skill follows. Where the text runs a\n"
        "> shell command against a skill script, call the matching MCP tool\n"
        "> instead -- do not run the script yourself. The server resolves the\n"
        "> skill scripts, the COVER installation and the cached Open API\n"
        "> credentials on its own.\n>\n"
        "> **Arguments.** A project path (`--project-root`, `$(pwd)`) is the\n"
        "> `projectDir` argument (absolute). Resume a build with `jobContext`.\n"
        "> The Open API server URL is `openApiServerUrl`. Never pass or print\n"
        "> the Access Key: it is read from the Local Agent's cached credentials\n"
        "> or `COVER_ACCESS_KEY`.\n>\n"
        "> **Not a tool.** Editing build files, running an IDE, or installing a\n"
        "> build tool is not done by this server. If you have shell or file-edit\n"
        "> tools, use them; otherwise give the user the exact command, file and\n"
        "> value and wait for them to confirm. Do not claim work you did not do.\n>\n"
        "> **Developer shells.** A Visual Studio / MSVC build needs the MCP\n"
        "> client to have been started from a Developer Command Prompt. If\n"
        "> compiler checks fail for that reason, ask the user to restart the\n"
        "> client from one -- it is not a path bug.\n\n"
        "%s\n\n---\n\n" % (topic, header)
    )


def read_guide(topic):
    """(text, error) -- exactly one is set."""
    if topic not in TOPICS:
        return None, "unknown topic: %s (one of %s)" % (
            topic, ", ".join(sorted(TOPICS)))
    skill_id, filename = TOPICS[topic]
    skill_dir = _skill_dir(skill_id)
    if skill_dir is None:
        return None, ("the %s skill is not installed next to this MCP server "
                      "(looked under %s)" % (skill_id, skills_root()))
    path = skill_dir / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, "could not read %s (%s)" % (path, exc)
    return _preamble(topic) + _FRONTMATTER.sub("", text, count=1).lstrip(), None


# --------------------------------------------------------------------------
# prompts -- the entry points a user picks from the client's prompt menu.
# --------------------------------------------------------------------------

PROJECT_ARG = {"name": "projectDir", "required": False,
               "description": "Absolute path of the project. Omit it and the "
                              "assistant will ask."}

PROMPTS = [
    {
        "name": "coverage-build",
        "title": "Run a COVER coverage build",
        "description": "Plan and run a COVER EE coverage build for a project, "
                       "following COVERA's build procedure.",
        "arguments": [PROJECT_ARG],
        "text": (
            "Run a COVER coverage build for {target}.\n\n"
            "First read `covera_get_guide` topic=build and follow that "
            "procedure. Plan first (covera_coverage_build without execute). "
            "Before running with execute, explain what it does -- it changes "
            "the project's build setup and registers the module on the shared "
            "COVER server -- and get my approval. If a build is already in "
            "progress, resume it with its jobContext instead of starting over."),
    },
    {
        "name": "coverage-measure",
        "title": "Look up module coverage",
        "description": "Read a module's coverage results from the COVER server.",
        "arguments": [
            {"name": "module", "required": False,
             "description": "Module name (filename or path suffix) or 24-hex "
                            "module id. Omit it and the assistant will ask."},
        ],
        "text": (
            "Look up the COVER coverage results for {target}.\n\n"
            "Use `covera_coverage_measure`. If the server returns several "
            "candidates for a name, show them and ask which module I mean. "
            "Report the summary, and the per-file or per-function detail if I "
            "ask for it."),
    },
    {
        "name": "toolchain-recovery",
        "title": "Recover a COVER analysis error",
        "description": "Recover C/C++ files excluded by a COVER analysis error, "
                       "following COVERA's recovery procedure.",
        "arguments": [PROJECT_ARG],
        "text": (
            "Recover the COVER analysis error for {target}.\n\n"
            "First read `covera_get_guide` topic=recovery and follow it. Start "
            "with covera_recovery_discover / _start to gather the error "
            "material, verify a candidate on the server, and only apply the "
            "server change (covera_recovery_publish) after telling me what it "
            "will change and getting my approval. Do not modify original "
            "sources."),
    },
]


def prompt_listing():
    return [{k: p[k] for k in ("name", "title", "description", "arguments")}
            for p in PROMPTS]


def get_prompt(name, arguments):
    """(result, error)."""
    prompt = next((p for p in PROMPTS if p["name"] == name), None)
    if prompt is None:
        return None, "Unknown prompt: %s" % name
    arguments = arguments or {}
    value = arguments.get("projectDir") or arguments.get("module")
    target = ("`%s`" % value) if value else (
        "my project (no path was given -- first ask me for the absolute path of "
        "the project; do not assume the working directory)")
    return {"description": prompt["description"],
            "messages": [{"role": "user", "content": {
                "type": "text", "text": prompt["text"].format(target=target)}}]
            }, None
