# Codex Setup

Verified on 2026-05-06 using official OpenAI Codex docs and local `codex mcp --help` / `codex mcp add --help`.

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

Preferred CLI form:

```bash
codex mcp add jenkins -- jenkins-mcp-server
codex mcp list
```

If you need to pin the Python executable from this pyenv virtualenv:

```bash
codex mcp add jenkins -- /Users/mth/.pyenv/versions/venv3144/bin/python -m jenkins_mcp_server
```

Manual TOML form in `~/.codex/config.toml`:

```toml
[mcp_servers.jenkins]
command = "/Users/mth/.pyenv/versions/venv3144/bin/python"
args = ["-m", "jenkins_mcp_server"]
```

Do not put real Jenkins secrets in this file. Export them in the shell that starts Codex, or use your normal shell/profile secret handling.

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
