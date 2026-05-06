# Gemini Setup

Verified on 2026-05-06 using official Gemini CLI MCP docs and local `gemini mcp --help`, `gemini mcp add --help`, and `gemini skills --help`.

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

CLI form:

```bash
gemini mcp add jenkins /Users/mth/.pyenv/versions/venv3144/bin/python -m jenkins_mcp_server
gemini mcp list
```

Equivalent project `settings.json` shape:

```json
{
  "mcpServers": {
    "jenkins": {
      "command": "/Users/mth/.pyenv/versions/venv3144/bin/python",
      "args": ["-m", "jenkins_mcp_server"],
      "timeout": 30000
    }
  }
}
```

Do not put real Jenkins secrets in this file. Export them in the shell that starts Gemini, or use your normal shell/profile secret handling.

Workspace bundle downloads need separate env vars:

```bash
export JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1
export JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR="/absolute/path/with/enough/disk"
```

## Skills

The canonical skills live in `.agents/skills/`. Local Gemini CLI help supports `gemini skills install <source>` and `gemini skills link <path>`. This repo also includes `.gemini/skills/` compatibility symlinks/copies when needed by a Gemini workspace.
