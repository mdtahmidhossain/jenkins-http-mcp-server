# Jenkins MCP Server

[![CI](https://github.com/mdtahmidhossain/jenkins-http-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/mdtahmidhossain/jenkins-http-mcp-server/actions/workflows/ci.yml)
[![Coverage: 100%](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/mdtahmidhossain/jenkins-http-mcp-server/actions/workflows/ci.yml)

External Python MCP server source-validated against Jenkins 2.579. It connects through normal Jenkins HTTP APIs using the permissions available to `JENKINS_USER` and `JENKINS_API_TOKEN`.

It does not require Jenkins administrator access, does not install Jenkins plugins, and does not depend on the official Jenkins MCP Server Plugin.

## Python Setup

This project uses the latest stable Python 3.14.x listed by pyenv when last checked on 2026-08-30:

- Python: `3.14.7`
- pyenv virtualenv: `venv3147`

To reproduce:

```bash
pyenv install -s 3.14.7
pyenv virtualenvs --bare | grep -qx venv3147 || pyenv virtualenv 3.14.7 venv3147
pyenv local venv3147
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
```

For authenticated access, set both credentials. Leaving both unset uses Jenkins anonymous access:

```bash
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
`JENKINS_URL` must be an absolute HTTP(S) URL without embedded credentials, a query, or a fragment.
Download directory settings must be absolute paths.

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

Workspace starts use a REST guard around Jenkins' dynamic job-level `/ws` endpoint:

- They inspect the job and queue, then wait while the job is queued, building, or in
  post-processing. Status reports the current wait phase.
- A stable `lastBuild` number anchors the capture. An explicit older build is rejected with
  `workspace_build_not_current`; use archived artifacts for historical build files.
- Jenkins state is checked before, during, and after the `/ws` transfer. If it changes, the partial
  output is deleted and the capture is retried once. A second change fails clearly.
- Matching callers on the same machine join one operation through a SQLite registry under the
  configured download root. Detached workers continue if the initiating MCP process exits.
- A completed matching capture is reused only while its anchor still equals the current stable
  `lastBuild` and all required local files exist. Pass `force_refresh=true` to bypass reuse.
- Start responses identify the result as `started`, `joined`, or `reused`. Poll
  `jenkins_get_workspace_bundle_status` for bytes, speed, phase, and final paths.

For a full workspace anchored to build `123`, the temporary archive is named `<safe-job>123.zip`,
for example `my-job123.zip`. It is extracted under `workspace/` and deleted after successful
extraction. The exact run console is saved as `<safe-job>123-console.log`.

Local output is grouped first by Jenkins job and then by build number:

```text
<workspace-download-root>/
└── my-job/
    └── 123/
        ├── workspace/
        ├── my-job123-console.log
        └── metadata.json
```

If that build directory already exists and cannot be reused, the new directory receives the
operation ID suffix, for example `my-job/123-a4f720c1/`.

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
- Newly reserved build and artifact output directories use owner-only `0700` permissions.
- Cancelling an already terminal local download is a no-op and reports `cancel_requested=false`.

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
- Jenkins core evidence for `jenkins_stop_build` is `AbstractBuild.doStop`. Plugin-defined run types,
  including Pipeline runs, may expose different stop behavior; Jenkins 404/403 responses are returned
  clearly rather than treated as success.
- Jenkins 2.574 stopped bundling JUnit. Controllers that do not already have the JUnit plugin may not expose `testReport`.
- Jenkins 2.579 removed Apache Commons Lang 2 from core. Update installed plugins before upgrading Jenkins because outdated plugins that relied on the core-provided library may fail to load.
- Jenkins 2.579 reserializes job configuration for `GET config.xml` and after `POST config.xml`. Config XML formatting, comments, and element ordering are not guaranteed to round-trip byte-for-byte.
- Nested folder paths are URL-encoded as repeated `job/<segment>` path components. Controllers without the needed folder/job type return Jenkins 404s.
- Workspace downloads use Jenkins' dynamic job-level `/ws` endpoint. Jenkins core can select some
  available workspace and exposes no workspace build/version identity in that response. The REST
  guard reduces races but cannot make `/ws` an immutable or transactionally consistent build
  snapshot; `workspace_freshness` is therefore `best_effort`.
- Exact historical files require archived build artifacts. Passing a build other than the current
  stable `lastBuild` to a workspace start is rejected.
- Core `/ws` evidence applies to `AbstractProject`. Other job types may expose workspace behavior
  through plugins; unsupported controllers or job types return Jenkins 404/permission errors.
- Workspace operations stream to disk and report status/progress through `jenkins_get_workspace_bundle_status`; large downloads can still stress Jenkins controllers or agents.
- Workspace workers are detached from the initiating STDIO MCP process. Multiple local MCP clients
  share operation state through SQLite. If a worker dies, a later status/start operation marks its
  stale capture failed and removes its retained output before replacement.
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

Optional tag-based GitHub Releases are documented in `docs/releasing.md`. Release artifacts are
attached to GitHub and are not published to PyPI. The source is licensed under the MIT License; see
`LICENSE` and `SECURITY.md`.

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
