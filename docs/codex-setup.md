# Codex Setup

Verified on 2026-08-30 using the official OpenAI Codex MCP documentation and local
`codex-cli 0.151.0` help.

## Install

From this repository:

```bash
python -m pip install -e .
```

Set Jenkins credentials outside Codex config:

```bash
export JENKINS_URL="https://jenkins.example.com/"
export JENKINS_USER="your-user"
export JENKINS_API_TOKEN="your-api-token"
```

## Add STDIO Server

Add the STDIO command with the CLI:

```bash
codex mcp add jenkins -- /Users/mth/.pyenv/versions/venv3147/bin/python -m jenkins_mcp_server
codex mcp list
```

Then use the manual TOML form in `~/.codex/config.toml` to forward variable names without storing
their values:

```toml
[mcp_servers.jenkins]
command = "/Users/mth/.pyenv/versions/venv3147/bin/python"
args = ["-m", "jenkins_mcp_server"]
env_vars = [
  "JENKINS_URL",
  "JENKINS_USER",
  "JENKINS_API_TOKEN",
  "JENKINS_VERIFY_SSL",
  "JENKINS_TIMEOUT_SECONDS",
  "JENKINS_MCP_MAX_RESPONSE_BYTES",
  "JENKINS_MCP_MAX_LOG_BYTES",
  "JENKINS_MCP_MAX_LOG_SCAN_BYTES",
  "JENKINS_MCP_ENABLE_WRITES",
  "JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE",
  "JENKINS_MCP_ENABLE_DELETE",
  "JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD",
  "JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR",
  "JENKINS_MCP_MAX_WORKSPACE_ARCHIVE_BYTES",
  "JENKINS_MCP_MAX_WORKSPACE_EXTRACT_BYTES",
  "JENKINS_MCP_MAX_WORKSPACE_FILES",
  "JENKINS_MCP_MAX_BUNDLE_LOG_BYTES",
  "JENKINS_MCP_WORKSPACE_PROGRESS_INTERVAL_SECONDS",
  "JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD",
  "JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR",
  "JENKINS_MCP_MAX_ARTIFACT_BYTES",
  "JENKINS_MCP_ARTIFACT_PROGRESS_INTERVAL_SECONDS",
]
```

Official Codex configuration supports `env_vars` specifically to allow and forward existing local
environment variables. Do not use a static `[mcp_servers.jenkins.env]` token value, and do not pass an
expanded token through `codex mcp add --env`; either form would persist the value in config.

## Enabling Writes

Keep writes disabled unless the user explicitly asks for an action:

```bash
export JENKINS_MCP_ENABLE_WRITES=1
```

Job config writes and deletes need additional flags documented in `docs/security.md`.

Workspace bundle downloads need separate env vars:

```bash
export JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1
export JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR="/absolute/path/with/enough/disk"
```

Individual artifact downloads use a separate gate:

```bash
export JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD=1
export JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR="/absolute/path/for/artifacts"
```
