# Privacy Policy — Suresoft COVER plugin (covera)

This policy covers the `covera` plugin: its skills, the Python scripts they run, and the bundled Eclipse CDT collection plugin. It does not cover COVER EE or its Local Agent, which are separate Suresoft Technologies products with their own terms and data handling.

**Last updated:** 2026-10-16

## What is collected

The plugin collects nothing for Suresoft Technologies. It has no telemetry, analytics, crash reporting, or update checks.

To work, it reads the following on your machine:

- The environment variables `CODESCROLL_TM_INSTALL_PATH` (Local Agent installation) and `CODESCROLL_TM_SERVER_URL` (the Local Agent's remote-service address).
- The COVER Access Key, from the `COVER_ACCESS_KEY` environment variable, from what you enter in the session, or from the credential cache described below.
- The COVER Open API main web server URL, from what you enter or from the credential cache.
- For Java, the JACOV settings in `COVER_JACOV_CONFIG` or `~/cover_jacov.json`.
- Project files, build logs, and toolchain information of the project you ask it to build, and, for collection, the memory of the STM32 debug session you select.

## How data is used and stored

- **Credential cache:** after the first successful server authentication, the plugin saves the Open API server URL and Access Key in plain text to `<CODESCROLL_TM_INSTALL_PATH>/agent_kit/open-api-credentials.json`, so you aren't asked again. The file is created once with owner-only permissions (`0600`) and is never overwritten automatically. On Windows it inherits the access rights of the installation folder, so if other accounts can read that folder they may be able to read the file. Delete it to stop reuse.
- **Java settings:** when no JACOV settings exist, the plugin creates `~/cover_jacov.json` with the Java key and server URL that COVER uses.
- **Access Key elsewhere:** the Access Key is passed to `curl` through standard input. It never goes into command arguments, job files, logs, or reports.
- **Project changes:** after a verified build, the plugin creates a `.cover-ee/` folder in the project root. It holds the settings, the applied state, backups of the original files, and entry points for manual runs. Depending on the environment, the plugin imports `.cover-ee/config/cover-msbuild.props` into the selected Visual Studio `.vcxproj` configurations, keeps the C# connection for the next rebuild, or updates the STM32CubeIDE build configuration (`PATH`, `COVER_BR_CONFIG`, and wrapper paths for tool commands) and the Agent's `gcc.config`, keeping the original as `gcc.config.cover-ee-backup`. CMake, Make, Maven, and Gradle builds are run by injecting options on the command line, and their build files are not modified.
- **Job records:** build plans, logs, and evidence are stored in the job's working folder.
- **IDE plugin:** coverage collection installs the bundled `com.qualityscroll.cover.collect` plugin into your STM32CubeIDE installation the first time it is needed, and the IDE must be restarted afterward. The plugin's Java source and file hashes are included in this repository.
- **Toolchain recovery:** diagnosis uses the analysis error data you provide or that is fetched from the server. The COVER PA used for verification is not distributed with this plugin; when you provide one, verification runs on copies.

## What leaves your machine

- **COVER Open API server**, at the main web server URL you provide: the Access Key in the `Authorization` header, and the build paths, file names, tool information, and instrumentation analysis information for the job. Toolchain recovery also reads analysis error data from the server. Only when a fix is verified with a PA you provide does recovery register the new toolchain configuration on the server and connect it to the wrapper, keeping a rollback record.
  By default, `curl` calls this server **without certificate verification** (`-k`), because COVER deployments often have no internal CA. Use `--verify-tls` to require verification.
- **COVER Local Agent / remote service**, at `CODESCROLL_TM_SERVER_URL`: coverage build and measurement data, sent through the Local Agent that COVER provides. When this address is HTTPS, the server certificate first presented during a job is trusted for that job only, and hostname checks remain on.
- **Collected coverage files:** verified dump files are delivered to the Local Agent's monitoring folder. From there, the Agent may upload them to the COVER server.

Separately, the plugin's output is shown in your Claude session, including file paths, build logs, and coverage results. Your Claude client sends it to the AI model provider under that provider's privacy policy (for Claude, Anthropic's privacy policy).

## Third parties

Suresoft Technologies receives nothing from this plugin and shares no data with third parties. Whoever operates the COVER servers you configure governs the data sent to them.

## Retention

The plugin keeps no data of its own beyond the local files listed above, which remain until you delete them. Remove server-side data through COVER.

## Changes to this policy

Changes are tracked in this repository's commit history.

## Contact

Suresoft Technologies — [bizcenter@suresofttech.com](mailto:bizcenter@suresofttech.com) — <https://www.suresofttech.com>
