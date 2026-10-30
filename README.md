# COVERA — AI Agent for COVER Code Coverage

> 한국어: [README.ko.md](README.ko.md)

COVERA (COVER AI Agent) is an AI agent that lets you use COVER code coverage in Claude Code. Describe what you want, such as "make a coverage build of this project", and COVERA identifies the project's original build for C/C++, C#, and Java across CMake, Make, Visual Studio/MSBuild, STM32CubeIDE, C# (COVER Toolbox), and Java (JACOV CLI, Maven, Gradle). It then runs the COVER instrumented build, verifies that the instrumented outputs were produced at the original paths, and keeps the verified setup so you can rebuild later from your IDE or the command line. It also collects coverage memory dumps from STM32CubeIDE debug sessions and diagnoses C/C++ analysis errors with a proposed toolchain configuration fix.

COVER is a code coverage measurement tool that verifies test sufficiency by quantitatively analyzing scenario test execution results.

COVERA does not replace COVER's results. COVER performs the instrumentation and measurement and produces the coverage results; COVERA prepares, runs, verifies, and reports that work from inside the coding session.

## What COVER and COVERA add

| Need | COVER and COVERA workflow |
|---|---|
| Make a coverage build without learning each toolchain's setup | Finds the original build and actual tools for CMake, Make, Visual Studio/MSBuild, STM32CubeIDE, C# (COVER Toolbox), and Java (JACOV CLI, Maven, Gradle), then runs and verifies the instrumented build. |
| Reuse the setup later | Keeps the verified setup for your IDE's next rebuild, or gives you a verified command to run again. |
| Collect coverage from embedded tests | After you test instrumented firmware in an STM32CubeIDE debug session, dumps the coverage memory, verifies the CVD file, and delivers it to the Local Agent. |
| Fix files excluded by analysis errors | Gets the error data, finds the cause, and proposes a parser configuration fix. Verifying and applying the fix needs the COVER PA, which this plugin doesn't include. |

## Workflow at a glance

1. Ask for a coverage build of your project. The first time, Claude asks for your COVER Access Key and the COVER Open API main web server URL.
2. Claude surveys the project and environment, runs the original build, then runs the instrumented build and checks its outputs.
3. Claude applies the verified setup and tells you how to rebuild from your IDE or command line.
4. For STM32 targets, run your tests in STM32CubeIDE, then ask Claude to collect the coverage data.

The skills in this repository work with a COVER EE Local Agent installation. They are not a replacement for COVER.

## Install

Add COVERA from Anthropic's plugin directory on claude.ai (**Customize > Plugins**). It loads in Claude Code through account sync. To try a local copy without installing, load it for one session from the folder that contains your checkout:

```text
claude --plugin-dir ./covera-plugin
```

COVERA is for Claude Code: its scripts run on your machine against a local COVER EE Local Agent and your build tools. Without COVER, or when its settings are missing, a skill stops at its setup check and says what is missing.

## Example requests

Responses can vary with the AI model and the conversation. If the result isn't what you expected, rephrase the request with more detail.

| Request | Skill |
|---|---|
| `Make a COVER coverage build of this project.` | `cover-ee-coverage-build` |
| `Set up coverage for the Debug configuration of this STM32CubeIDE project.` | `cover-ee-coverage-build` |
| `Make a coverage build of this Maven project with JACOV.` | `cover-ee-coverage-build` |
| `I finished testing in STM32CubeIDE. Collect the coverage data.` | `cover-ee-coverage-collect` |
| `Some files were excluded from the coverage build because of analysis errors. Find out why.` | `cover-toolchain-recovery` |

## Skills

All skills need a COVER EE Local Agent installation for their product-backed actions. Claude picks the skill from your request; you can also run one directly, for example `/covera:cover-ee-coverage-build`.

| Skill | Workflow stage |
|---|---|
| `cover-ee-coverage-build` | Prepare, run, and verify a COVER EE coverage build, then keep the verified setup for later builds. |
| `cover-ee-coverage-collect` | Collect the coverage memory dump from an STM32CubeIDE debug session, verify the CVD file, and deliver it to the Local Agent. |
| `cover-toolchain-recovery` | Diagnose C/C++ files excluded by analysis errors: get the error data and propose a parser configuration fix. The COVER PA (`tmpa`) that verifies the fix is not distributed with this plugin, so by default the skill stops at diagnosis and the proposed fix (`GuidanceOnly`). If you have a PA and pass its path with `--pa-root`, it verifies the fix on copies, registers it on the COVER server, and connects the wrapper. |

## Product-backed workflow

COVERA is used where a COVER EE Local Agent is installed. This repository contains the skills, the scripts they run, and a coverage collection plugin for STM32CubeIDE. It does not include COVER itself, and it does not report instrumentation or measurement as done without COVER.

What changes on your machine during the work:

- **Builds.** Runs your original build and the COVER instrumented build with your project's build tools.
- **Project files.** After a verified build, COVERA creates a `.cover-ee/` folder in the project root for its settings and backups of the original files. Visual Studio, C#, and STM32CubeIDE projects get their project settings changed so later builds keep coverage, and the Local Agent's settings are updated when needed. CMake, Make, Maven, and Gradle build files are not modified.
- **IDE plugin.** Coverage collection installs the bundled plugin into STM32CubeIDE the first time it is needed. Restart the IDE afterward.

## Requirements

- COVER EE Local Agent installed and licensed, with `CODESCROLL_TM_INSTALL_PATH` pointing to the installation and `CODESCROLL_TM_SERVER_URL` to its remote service
- A COVER Access Key and the COVER Open API main web server URL (HTTPS)
- Python 3.11 or later and curl on `PATH`
- The project's own build tools: CMake/Make and GCC, Visual Studio/MSBuild, STM32CubeIDE, the .NET toolchain, or Java with Maven or Gradle
- For coverage collection: STM32CubeIDE with an active Debug session
- Windows or Linux; WSL is supported when the Local Agent runs on Windows

For a product demo, purchase, or deployment consultation, contact [bizcenter@suresofttech.com](mailto:bizcenter@suresofttech.com).

## Privacy Policy

COVERA's scripts run on your machine.

- COVERA sends nothing to Suresoft Technologies and has no telemetry.
- It connects only to the COVER server you provide, with your Access Key, and registers this machine's Local Agent and its toolchains on the server when needed. By default, the certificate is not verified, because COVER servers often use internal certificates. Pass `--verify-tls` to require verification.
- After the first successful authentication, the server URL and Access Key are saved in plain text to `<CODESCROLL_TM_INSTALL_PATH>/agent_kit/open-api-credentials.json` so you aren't asked again. Delete the file to stop reuse. The Access Key is never written to logs.
- For Java, `~/cover_jacov.json` is created when missing.
- Its output, such as file paths, build logs, and coverage results, appears in your Claude session and is sent to the AI model provider by your Claude client.
- Data that COVER itself keeps follows COVER's own data handling policy.

The full policy is in [PRIVACY.md](PRIVACY.md). For support and questions, contact [bizcenter@suresofttech.com](mailto:bizcenter@suresofttech.com).

## License and trademarks

This repository is licensed under the [MIT License](LICENSE). The license covers everything in this repository: the documentation, the skill files, the scripts, and the bundled IDE collection plugin. It does not cover COVER EE or its Local Agent. COVER and COVERA are Suresoft Technologies products, and no product functionality or product license is granted by this repository.

STM32CubeIDE is a trademark of STMicroelectronics. Claude and Claude Code are trademarks of Anthropic. Other product names may be trademarks of their respective owners.
