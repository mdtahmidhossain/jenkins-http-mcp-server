# Jenkins MCP Server

External Python MCP server for Jenkins 2.563. It connects through normal Jenkins HTTP APIs using the permissions available to `JENKINS_USER` and `JENKINS_API_TOKEN`.

It does not require Jenkins administrator access, does not install Jenkins plugins, and does not depend on the official Jenkins MCP Server Plugin.

## Python Setup

This project was initialized with pyenv using the latest stable Python 3.14.x available locally:

- Python: `3.14.4`
- pyenv virtualenv: `venv3144`

To reproduce:

```bash
pyenv local venv3144
python --version
which python
pyenv version
```

## Install

```bash
python -m pip install -e '.[dev]'
```

## Environment

Required:

```bash
export JENKINS_URL="https://jenkins.example.com/"
export JENKINS_USER="your-user"
export JENKINS_API_TOKEN="your-api-token"
```

Optional:

```bash
export JENKINS_VERIFY_SSL=1
export JENKINS_TIMEOUT_SECONDS=30
export JENKINS_MCP_MAX_RESPONSE_BYTES=2000000
export JENKINS_MCP_MAX_LOG_BYTES=200000
```

Workspace bundle downloads are gated separately because they can be very large and may contain
secrets or other untrusted files:

```bash
export JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1
export JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR="/absolute/path/with/enough/disk"
export JENKINS_MCP_MAX_WORKSPACE_ARCHIVE_BYTES=6000000000
export JENKINS_MCP_MAX_WORKSPACE_EXTRACT_BYTES=20000000000
export JENKINS_MCP_MAX_WORKSPACE_FILES=200000
export JENKINS_MCP_MAX_BUNDLE_LOG_BYTES=1200000000
export JENKINS_MCP_WORKSPACE_PROGRESS_INTERVAL_SECONDS=2
```

Write gates:

```bash
export JENKINS_MCP_ENABLE_WRITES=1
export JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE=1
export JENKINS_MCP_ENABLE_DELETE=1
```

Do not store real Jenkins secrets in MCP client config files.

## Run STDIO Server

```bash
python -m jenkins_mcp_server
```

Console script:

```bash
jenkins-mcp-server
```

## Client Setup

- Codex CLI: `docs/codex-setup.md`
- Gemini CLI: `docs/gemini-setup.md`

## Tools

Read-only:

- `jenkins_whoami`
- `jenkins_version`
- `jenkins_health`
- `jenkins_get_json`
- `jenkins_list_jobs`
- `jenkins_get_job`
- `jenkins_get_job_config`
- `jenkins_list_builds`
- `jenkins_get_build`
- `jenkins_get_build_log`
- `jenkins_get_build_artifacts`
- `jenkins_get_test_report`
- `jenkins_list_queue`
- `jenkins_get_queue_item`
- `jenkins_list_views`
- `jenkins_get_view`
- `jenkins_list_nodes`
- `jenkins_get_node`
- `jenkins_list_plugins`

Workspace bundle tools, gated by `JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1` and
`JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR`:

- `jenkins_start_workspace_bundle_download`
- `jenkins_get_workspace_bundle_status`
- `jenkins_cancel_workspace_bundle_download`

Write tools, gated by `JENKINS_MCP_ENABLE_WRITES=1`:

- `jenkins_trigger_build`
- `jenkins_trigger_build_with_parameters`
- `jenkins_stop_build`
- `jenkins_cancel_queue_item`
- `jenkins_enable_job`
- `jenkins_disable_job`

Optional job config tools, gated by `JENKINS_MCP_ENABLE_WRITES=1` and `JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE=1`:

- `jenkins_create_job`
- `jenkins_copy_job`
- `jenkins_update_job_config`

Delete additionally requires `JENKINS_MCP_ENABLE_DELETE=1`:

- `jenkins_delete_job`

## Safety

- Read-only by default.
- Write tools require explicit local env flags and Jenkins-side permissions.
- Workspace bundle downloads require a separate explicit env flag and output directory.
- Jenkins logs and job output are treated as untrusted text.
- Jenkins workspace files are treated as untrusted local files.
- API tokens and Authorization headers are not printed by server helpers.
- 401, 403, 404, crumb failures, and permission failures return structured errors.

## Limitations

- No script console.
- No restart, safe restart, or quiet down.
- No plugin install/update.
- No credential read/write.
- No node creation/deletion.
- No global config changes.
- No user management.
- `jenkins_get_test_report` depends on a test-report plugin such as JUnit exposing `testReport`; it fails clearly if absent.
- Nested folder paths are URL-encoded as repeated `job/<segment>` path components. Controllers without the needed folder/job type return Jenkins 404s.
- Workspace bundle downloads use Jenkins' job-level workspace endpoint. The saved console log is build-run-specific, but the workspace is the current/some available job workspace and may not be an immutable snapshot of that build.
- Workspace bundle operations stream to disk and report status/progress through `jenkins_get_workspace_bundle_status`; large downloads can still stress Jenkins controllers or agents.

## Testing

Normal tests are mocked and do not require a live Jenkins controller:

```bash
python -m pytest
python -m compileall src
ruff check
```

Optional integration tests only run when all are set:

```bash
export JENKINS_INTEGRATION_TESTS=1
export JENKINS_URL="https://jenkins.example.com/"
export JENKINS_USER="your-user"
export JENKINS_API_TOKEN="your-api-token"
python -m pytest tests/test_integration.py
```

## Evidence Docs

- `docs/source-truth.md`
- `docs/source-skills-check.md`
- `docs/existing-research.md`
- `docs/architecture-decision.md`
- `docs/tool-evidence.md`
- `docs/security.md`
