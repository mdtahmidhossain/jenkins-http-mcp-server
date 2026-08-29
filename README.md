# Jenkins MCP Server

[![CI](https://github.com/mdtahmidhossain/jenkins-http-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/mdtahmidhossain/jenkins-http-mcp-server/actions/workflows/ci.yml)
[![Coverage: 100%](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/mdtahmidhossain/jenkins-http-mcp-server/actions/workflows/ci.yml)

External Python MCP server source-validated against Jenkins 2.579. It connects through normal Jenkins HTTP APIs using the permissions available to `JENKINS_USER` and `JENKINS_API_TOKEN`.

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
export JENKINS_MCP_MAX_LOG_SCAN_BYTES=1200000000
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

Individual artifact downloads have their own local gate and output directory:

```bash
export JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD=1
export JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR="/absolute/path/for/artifacts"
export JENKINS_MCP_MAX_ARTIFACT_BYTES=6000000000
export JENKINS_MCP_ARTIFACT_PROGRESS_INTERVAL_SECONDS=2
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
- `jenkins_get_build_log_chunk`
- `jenkins_search_build_log`
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
- `jenkins_start_workspace_path_download`
- `jenkins_get_workspace_bundle_status`
- `jenkins_cancel_workspace_bundle_download`
- `jenkins_cleanup_workspace_bundle_operations`

`jenkins_start_workspace_path_download` downloads one workspace `file` or one
workspace `folder` plus the selected build run's console log. Folder downloads
are extracted locally and the zip archive is deleted after successful extraction.

Artifact download tools, gated by `JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD=1` and
`JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR`:

- `jenkins_start_artifact_download`
- `jenkins_get_artifact_download_status`
- `jenkins_cancel_artifact_download`

Artifact files stream directly to disk. MCP responses contain progress and local paths, not file
contents or base64 data.

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
- Workspace and workspace-path downloads require a separate explicit env flag and output directory.
- Artifact downloads require their own explicit env flag and output directory.
- Jenkins logs and job output are treated as untrusted text.
- Jenkins workspace files are treated as untrusted local files.
- API tokens and Authorization headers are not printed by server helpers.
- 401, 403, 404, crumb failures, and permission failures return structured errors.
- HTTP response limits are enforced while bytes arrive. Transient GET failures are retried up to
  three attempts; POST requests are never automatically replayed.
- Large local downloads preflight free disk space, use partial paths, and remove failed partials.

## Limitations

- No script console.
- No restart, safe restart, or quiet down.
- No plugin install/update.
- No credential read/write.
- No node creation/deletion.
- No global config changes.
- No user management.
- "Plugin-dependent" means Jenkins core does not guarantee that endpoint; it exists only when an
  installed plugin provides it. This server never installs or enables that plugin.
- `jenkins_get_test_report` depends on a test-report plugin such as JUnit exposing `testReport`; it fails clearly if absent.
- Jenkins 2.574 stopped bundling JUnit. Controllers that do not already have the JUnit plugin may not expose `testReport`.
- Jenkins 2.579 removed Apache Commons Lang 2 from core. Update installed plugins before upgrading Jenkins because outdated plugins that relied on the core-provided library may fail to load.
- Jenkins 2.579 reserializes job configuration for `GET config.xml` and after `POST config.xml`. Config XML formatting, comments, and element ordering are not guaranteed to round-trip byte-for-byte.
- Nested folder paths are URL-encoded as repeated `job/<segment>` path components. Controllers without the needed folder/job type return Jenkins 404s.
- Workspace downloads use Jenkins' job-level workspace endpoint. The saved console log is build-run-specific, but the workspace is the current/some available job workspace and may not be an immutable snapshot of that build.
- Workspace operations stream to disk and report status/progress through `jenkins_get_workspace_bundle_status`; large downloads can still stress Jenkins controllers or agents.
- Background download workers do not survive MCP server exit. A later status check marks an
  interrupted operation failed and removes its partial files; restart the download explicitly.
- Progressive log chunks must fit `JENKINS_MCP_MAX_LOG_BYTES`. If Jenkins has accumulated a larger
  interval than the limit, the tool returns `response_too_large` without advancing the cursor.
- Build artifacts are archived build outputs. Workspace files are separate, current job-level data.
- GET redirects are rejected instead of followed. Artifact-manager plugins that require an external
  storage redirect are unsupported and return a clear redirect error.

## Testing

Normal tests are mocked and do not require a live Jenkins controller:

```bash
python -m pytest
python -m compileall src
ruff check
```

`python -m pytest` reports missing lines in the terminal and writes `coverage.xml`.
GitHub Actions also publishes the coverage table in the workflow run summary. The test command
enforces 100% source line coverage locally and in CI.

## Releases

The package distribution name is `jenkins-http-mcp-server`. Release builds and tokenless PyPI
trusted publishing are documented in `docs/releasing.md`. The source is licensed under the MIT
License; see `LICENSE` and `SECURITY.md`.

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
