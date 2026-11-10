# Releasing the MCP bundle

The MCP server in `scripts/` is distributed as an MCP Bundle (`.mcpb`) attached
to a GitHub release and listed in the official MCP registry through
`server.json`. The Claude Code / Gemini plugin (skills, `.mcp.json`) is a
separate distribution with its own version in `plugin.json` /
`gemini-extension.json`, and is **not** affected by these steps.

The MCP server is versioned independently of the plugin. Its release tags are
prefixed `mcp-v` (for example `mcp-v0.1.0`); plugin releases use plain `v*`
tags and do not trigger the MCP workflow. This keeps the two cadences apart and
means a plugin bump never forces an MCP republish, nor the reverse.

**Releasing is automated.** A maintainer bumps the MCP version in the tracked
files and pushes an `mcp-v` tag. `.github/workflows/publish-mcp-registry.yml`
then builds the bundle, creates the GitHub release, uploads the bundle, hashes
it, and publishes `server.json` to the registry. There is no manual bundle
build or hash step.

## Per release

1. Bump the version to `X.Y.Z` in every MCP-owned file below and commit to
   `main`:

   - `server.json`
   - `manifest.json`
   - `pyproject.toml`
   - `SERVER_INFO` in `scripts/covera_mcp/server.py`

   The plugin manifests (`plugin.json`, `.claude-plugin/plugin.json`,
   `gemini-extension.json`) are **not** part of an MCP release. Run the suite
   first; `test_versions_agree` checks the MCP files match:

   ```
   python -m unittest discover -s tests
   ```

   To find any stragglers:

   ```
   grep -rn 'X\.Y\.Z' server.json manifest.json pyproject.toml \
     scripts/covera_mcp/server.py
   ```

2. Tag that commit and push the tag:

   ```
   git tag mcp-vX.Y.Z
   git push origin mcp-vX.Y.Z
   ```

3. Watch the workflow and confirm the registry entry:

   ```
   gh run watch "$(gh run list --workflow publish-mcp-registry.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.SuresoftTechnologies"
   ```

That is the whole release. The tag's version (`mcp-vX.Y.Z` -> `X.Y.Z`) must
match the tracked files or the workflow stops at its first step.

## What the workflow does

On a pushed `mcp-v*` tag (or `workflow_dispatch` with a tag):

1. **Version guard** - the tag must equal the version in `server.json`, and
   `manifest.json`, `pyproject.toml` and `SERVER_INFO` must all agree. A
   mismatch fails here, before anything is published.
2. **Pack** - `mcpb pack` builds `covera-mcp.mcpb`. The name contains `mcp`
   because the registry requires the package URL to. `.mcpbignore` decides the
   contents (see below).
3. **Release** - creates the GitHub release for the tag (if absent) and uploads
   the bundle.
4. **Hash** - computes `fileSha256` from the uploaded bundle and writes it, with
   the tag's download URL, into `server.publish.json` (a publish-only copy; the
   repo's `server.json` is never rewritten).
5. **Publish** - validates and publishes `server.publish.json` to the registry
   over GitHub OIDC.

`mcpb pack` is not reproducible - the same tree packs to different bytes each
time - so the bundle is built once in CI and that exact file is both released
and hashed. The registry does not check the hash; MCP clients do. Computing it
from the released bytes removes any chance of a stale hash reaching clients.

The `fileSha256` in the committed `server.json` is a permanent placeholder
(`0000...`). It is never the real hash: the real value exists only on the
release and is injected at publish time. Do not try to keep it current by hand.

## What goes in the bundle

`.mcpbignore` decides this. The bundle keeps `scripts/` (the MCP server) and
`skills/` - each skill's entry scripts are the runtime the server drives, and
`covera_get_guide` serves the `SKILL.md` and reference text. It drops the plugin
surface (`.claude-plugin/`, `plugin.json`, `gemini-extension.json`, `.mcp.json`,
`scripts/covera-mcp.cmd`) and repo tooling (`tests/`, `.github/`, this file).

## Why the `uv` server type

`manifest.json` declares `server.type: "uv"`, the one MCPB type where the host
provides Python ("Host application manages Python and dependencies
automatically", MCPB README). The server has no third-party dependencies, so uv
fetches its own CPython and runs `scripts/mcp_server.py` with nothing to
install. `python` would need an interpreter the user already has (and on Windows
the `python` alias is often the Microsoft Store stub); `node` would mean
rewriting the server; `binary` needs a per-platform build.

## The install path

The skills find the COVER EE Local Agent through `CODESCROLL_TM_INSTALL_PATH`,
which the Local Agent installation sets in the environment. The server reads it
from the inherited environment and does not override it; the bundle exposes no
install-path config of its own. When it is unset, `covera_doctor` reports it as
missing.

## Credentials

The Open API Access Key is never passed as a tool argument or printed. The
coverage-build flow reads it from the Local Agent's cached
`open-api-credentials.json` (or `COVER_ACCESS_KEY`). The recovery flow reads
`COVER_ACCESS_KEY` from the environment; when it is unset the server bridges the
cached key into the subprocess **only when the requested server matches the
cached Open API server** (`resolve_cover_key`). The key is used solely to set
the child process environment, locally, and is never logged or returned.

## Re-publishing a tag

`workflow_dispatch` re-runs the whole flow for an existing tag (pass the tag as
input). It re-uploads the bundle with `--clobber` and republishes. Prefer a new
version number over re-cutting one that is already public.

## One-time setup

- Actions enabled on the repository.
- Nothing else: OIDC needs no stored secret, and the bundle has no dependencies
  to configure.
