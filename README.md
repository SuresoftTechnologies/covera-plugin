# COVERA — AI Agent for COVER Code Coverage

> 한국어: [README.ko.md](README.ko.md)

COVERA (COVER AI Agent) is an AI agent that lets you use COVER code coverage in coding agents such as Claude Code, Codex CLI, Cursor, and GitHub Copilot. Describe what you want, such as "make a coverage build of this project", and COVERA identifies the project's original build for C/C++, C#, and Java across CMake, Make, Visual Studio/MSBuild, STM32CubeIDE, C# (COVER Toolbox), and Java (JACOV CLI, Maven, Gradle). It then runs the COVER instrumented build while keeping the original output paths, judges the result by the build's exit code, and keeps the successful setup so you can rebuild later from your IDE or the command line. It also looks up a module's coverage results stored on the COVER server, and recovers C/C++ files excluded by analysis errors by verifying a toolchain configuration fix on the COVER server.

COVER is a code coverage measurement tool that verifies test sufficiency by quantitatively analyzing scenario test execution results.

COVERA does not replace COVER's results. COVER performs the instrumentation and measurement and produces the coverage results; COVERA prepares, runs, verifies, and reports that work from inside the coding session.

## What COVER and COVERA add

| Need | COVER and COVERA workflow |
|---|---|
| Make a coverage build without learning each toolchain's setup | Finds the original build and actual tools for CMake, Make, Visual Studio/MSBuild, STM32CubeIDE, C# (COVER Toolbox), and Java (JACOV CLI, Maven, Gradle), then runs the instrumented build. |
| Reuse the setup later | Keeps the successful setup for your IDE's next rebuild, or gives you a command to run again. |
| Check coverage results | Looks up the latest cumulative coverage of a module stored on the COVER server, with per-file and per-function details on request. |
| Fix files excluded by analysis errors | Gets the error data, finds the cause, verifies a parser configuration fix by running the COVER PA on the COVER server, then registers the fixed toolchain and connects the wrapper so you can rebuild. |

## Workflow at a glance

1. Ask for a coverage build of your project. The first time, the coding agent asks for your COVER Access Key and the COVER Open API main web server URL.
2. The coding agent surveys the project and environment, runs the original build, then runs the instrumented build and checks its result.
3. The coding agent applies the successful setup and tells you how to rebuild from your IDE or command line.
4. After running your tests, ask the coding agent for a module's coverage to see the results calculated on the COVER server.

The skills in this repository work with a COVER EE Local Agent installation. They are not a replacement for COVER.

## Install

Add COVERA from Anthropic's plugin directory on claude.ai (**Customize > Plugins**). It loads in Claude Code through account sync. To try a local copy without installing, load it for one session from the folder that contains your checkout:

```text
claude --plugin-dir ./covera-plugin
```

COVERA's scripts run on your machine against a local COVER EE Local Agent and your build tools. Without COVER, or when its settings are missing, a skill stops at its setup check and says what is missing.

## Example requests

Responses can vary with the AI model and the conversation. If the result isn't what you expected, rephrase the request with more detail.

| Request | Skill |
|---|---|
| `Make a COVER coverage build of this project.` | `cover-ee-coverage-build` |
| `Set up coverage for the Debug configuration of this STM32CubeIDE project.` | `cover-ee-coverage-build` |
| `Make a coverage build of this Maven project with JACOV.` | `cover-ee-coverage-build` |
| `Show the coverage of the app.elf module and its least-covered functions.` | `cover-ee-coverage-measure` |
| `Some files were excluded from the coverage build because of analysis errors. Find out why.` | `cover-toolchain-recovery` |

## Skills

The coding agent picks the skill from your request; you can also run one directly, for example `/covera:cover-ee-coverage-build`.

| Skill | Workflow stage |
|---|---|
| `cover-ee-coverage-build` | Prepare and run a COVER EE coverage build, judge it by the build's exit code, then keep the successful setup for later builds. Needs the Local Agent. |
| `cover-ee-coverage-measure` | Read-only lookup of a module's latest cumulative coverage on the COVER server, with per-file and per-function results on request. It does not run tests, collect new data, or build. Needs only the COVER server URL and Access Key. |
| `cover-toolchain-recovery` | Recover C/C++ files excluded by analysis errors: get the error data from the COVER server, diagnose the cause, and verify a minimal parser configuration fix by running the COVER PA (`tmpa`) on the server. After a successful verification it registers a new toolchain on the server, connects the wrapper, and hands off for a rebuild. If you ask only for a diagnosis, it stops there. |

## MCP server

COVERA also ships an MCP server that exposes the same three skills to any MCP client — Claude Desktop, Cursor, VS Code, and others — not only to agents that load the skills. A client with no skills still gets the full flow: run a coverage build, look up a module's coverage, and recover a toolchain error. The procedure each skill follows is served back through a tool (`covera_get_guide`), so a skill-less client has the same guidance to work from.

It is listed in the official MCP registry as `io.github.SuresoftTechnologies/covera-mcp`, and is versioned independently of the plugin (its release tags are prefixed `mcp-v`).

Three ways to run it:

- **With the plugin.** Installing COVERA registers the server automatically (`.mcp.json`), so in Claude Code the tools are available alongside the skills.
- **As a bundle.** Download `covera-mcp.mcpb` from this repository's releases and open it with a client that installs MCP bundles. It resolves its own Python, so nothing has to be installed first.
- **Point a client at the interpreter.** Run `scripts/mcp_server.py` with Python 3.11 or later. The launcher `scripts/covera-mcp.cmd` picks a working interpreter on Windows and Linux.

The tools mirror the skills:

| Group | Tools |
|---|---|
| Read-only | `covera_doctor`, `covera_coverage_measure`, `covera_recovery_discover`, `covera_recovery_inspect`, `covera_get_guide` |
| Coverage build | `covera_coverage_build` (plan, then execute) |
| Toolchain recovery | `covera_recovery_start`, `_collect`, `_verify`, `_publish_plan`, `_publish` |

Three prompts, `coverage-build`, `coverage-measure` and `toolchain-recovery`, start the guided flows. A coverage build with `execute`, and applying a recovery (`covera_recovery_publish`), change the project's build setup or the shared COVER server, so they carry consent hints and ask for your approval first; read `covera_get_guide` before driving them. Without a COVER EE Local Agent the server still starts and `covera_doctor` reports what is missing, so it is safe to leave configured on any machine.

The bundle reads the Local Agent from `CODESCROLL_TM_INSTALL_PATH`. When that is not already set on the machine, supply the path through the bundle's `COVERA_INSTALL_PATH` configuration; an empty value leaves the machine's own setting untouched. The Open API Access Key is never passed as a tool argument or printed: the build flow reads it from the Local Agent's cached credentials or `COVER_ACCESS_KEY`, and the recovery flow reads `COVER_ACCESS_KEY` (or the cached key when the requested server matches the cached one).

## Product-backed workflow

COVERA is used where a COVER EE Local Agent is installed. This repository contains the skills and the scripts they run. It does not include COVER itself, and it does not report instrumentation or measurement as done without COVER.

What changes on your machine during the work:

- **Builds.** Runs your original build and the COVER instrumented build with your project's build tools.
- **Project files.** After a successful coverage build, COVERA creates a `.cover-ee/` folder in the project root for its settings and backups of the original files. Visual Studio, C#, and STM32CubeIDE projects get their project settings changed so later builds keep coverage, and the Local Agent's settings are updated when needed. CMake, Make, Maven, and Gradle build files are not modified.
- **COVER server.** Toolchain recovery registers a new toolchain configuration on the server and connects it to the wrapper, keeping a rollback record. Coverage lookup only reads from the server.

## Requirements

- COVER EE Local Agent installed and licensed, with `CODESCROLL_TM_INSTALL_PATH` pointing to the installation and `CODESCROLL_TM_SERVER_URL` to its remote service
- A COVER Access Key and the COVER Open API main web server URL (HTTPS). Coverage lookup and toolchain recovery also need a COVER server that provides the AI Agent Kit API (`/cover-open-api/ai-agent-kit/...`). Toolchain recovery reads the Access Key from the `COVER_ACCESS_KEY` environment variable.
- Python 3.11 or later and curl on `PATH`
- The project's own build tools: CMake/Make and GCC, Visual Studio/MSBuild, STM32CubeIDE, the .NET toolchain, or Java with Maven or Gradle
- Windows or Linux; WSL is supported when the Local Agent runs on Windows

For a product demo, purchase, or deployment consultation, contact [bizcenter@suresofttech.com](mailto:bizcenter@suresofttech.com).

## Privacy Policy

COVERA's scripts run on your machine.

- COVERA sends nothing to Suresoft Technologies and has no telemetry. The MCP server has none of its own either; it forwards each call to the local skill runtime and makes no network call of its own.
- It connects only to the COVER server you provide, with your Access Key, and registers this machine's Local Agent and its toolchains on the server when needed. Toolchain recovery sends the source path, this machine's MAC address, the compiler ID, and the proposed configuration to the server so the server can run the PA. By default, the certificate is not verified, because COVER servers often use internal certificates. Pass `--verify-tls` to require verification.
- After the first successful authentication, the server URL and Access Key are saved in plain text to `<CODESCROLL_TM_INSTALL_PATH>/agent_kit/open-api-credentials.json` so you aren't asked again. Delete the file to stop reuse. The Access Key is never written to logs.
- For Java, `~/cover_jacov.json` is created when missing.
- Its output, such as file paths, build logs, and coverage results, appears in your coding agent session and is sent to the AI model provider by your coding agent client.
- Data that COVER itself keeps follows COVER's own data handling policy.

The full policy is in [PRIVACY.md](PRIVACY.md). For support and questions, contact [bizcenter@suresofttech.com](mailto:bizcenter@suresofttech.com).

## License and trademarks

This repository is licensed under the [MIT License](LICENSE). The license covers everything in this repository: the documentation, the skill files, and the scripts. It does not cover COVER EE or its Local Agent. COVER and COVERA are Suresoft Technologies products, and no product functionality or product license is granted by this repository.

STM32CubeIDE is a trademark of STMicroelectronics. Claude and Claude Code are trademarks of Anthropic. Other product names may be trademarks of their respective owners.
