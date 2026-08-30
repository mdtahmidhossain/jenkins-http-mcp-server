# Gemini Setup

Verified on 2026-08-30 using official Gemini CLI MCP and Agent Skills documentation plus local
`gemini 0.55.1` help.

## Install

From this repository:

```bash
python -m pip install -e .
```

Set Jenkins credentials outside Gemini config:

```bash
export JENKINS_URL="https://jenkins.example.com/"
export JENKINS_USER="your-user"
export JENKINS_API_TOKEN="your-api-token"
```

## Add STDIO Server

CLI form, verified in an isolated Gemini configuration:

```bash
gemini mcp add --scope project \
  -e 'JENKINS_URL=$JENKINS_URL' \
  -e 'JENKINS_USER=$JENKINS_USER' \
  -e 'JENKINS_API_TOKEN=$JENKINS_API_TOKEN' \
  jenkins /Users/mth/.pyenv/versions/venv3147/bin/python -m jenkins_mcp_server
gemini mcp list
```

Equivalent project `settings.json` shape:

```json
{
  "mcpServers": {
    "jenkins": {
      "command": "/Users/mth/.pyenv/versions/venv3147/bin/python",
      "args": ["-m", "jenkins_mcp_server"],
      "env": {
        "JENKINS_URL": "$JENKINS_URL",
        "JENKINS_USER": "$JENKINS_USER",
        "JENKINS_API_TOKEN": "$JENKINS_API_TOKEN"
      },
      "timeout": 30000
    }
  }
}
```

Gemini redacts inherited environment names matching `*TOKEN*`, so merely exporting
`JENKINS_API_TOKEN` is not enough. The explicit `env` mapping is required; `$JENKINS_API_TOKEN` is a
runtime reference, not the token value. Add the same kind of explicit reference for any optional
setting that your Gemini environment does not inherit. Never hardcode a real token in this file.

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

## Skills

The canonical skills live in `.agents/skills/`, which current Gemini CLI recognizes as a workspace
skills alias. The `.gemini/skills/` symlinks remain compatible but are not required for discovery.
Workspace skills are loaded only for a trusted folder. In an interactive Gemini session, use
`/trust` for this repository if appropriate, restart, and then use `/skills list` (or
`gemini skills list`) to verify discovery.
