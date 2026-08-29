# Architecture Decision

## Decision

Use option B only: an external Python MCP server that connects to Jenkins through normal Jenkins HTTP APIs available to `JENKINS_USER` and `JENKINS_API_TOKEN`.

## Rationale

- The user does not have Jenkins administrator permission.
- The server must not require Jenkins plugin installation.
- Jenkins 2.579 core and official docs expose the needed HTTP endpoints through normal Remote Access API and Stapler web methods.
- The official Jenkins MCP Server Plugin is not usable here because it requires installation/enabling inside Jenkins.

## Transport

STDIO is implemented first using the official MCP Python SDK v2 `MCPServer`.

HTTP transport was not added because STDIO is the requested first target for Codex CLI and Gemini CLI. The SDK supports streamable HTTP, so it can be added later as a small launcher option if needed.

## Safety Model

- Default mode is read-only.
- Write tools require `JENKINS_MCP_ENABLE_WRITES=1`.
- Job config write tools require `JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE=1`.
- Delete requires `JENKINS_MCP_ENABLE_DELETE=1`.
- Workspace bundle downloads require `JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1` and a local `JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR`.
- Artifact downloads require `JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD=1` and a local
  `JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR`.
- Admin-like operations are not implemented: script console, restart, safe restart, quiet down, plugin install/update, credential read/write, node creation/deletion, global config changes, and user management.

## Long-Running Downloads

Workspace bundle downloads run asynchronously. `jenkins_start_workspace_bundle_download` resolves the build number, creates a local operation directory, and starts a background worker. Progress is written to `.progress.json` and returned by `jenkins_get_workspace_bundle_status`, including downloaded bytes, total bytes when Jenkins sends `Content-Length`, speed, elapsed seconds, phase, and paths. `jenkins_cancel_workspace_bundle_download` writes a cancel marker that the worker checks during download and extraction.

`jenkins_start_workspace_path_download` uses the same async operation model for one requested workspace file or folder. File downloads stream directly to disk. Folder downloads use Jenkins' workspace zip support, extract locally, and delete the archive after successful extraction.

Individual build artifacts use a separate async operation root and safety gate. Artifact bytes stream
to disk; MCP returns only status, progress, and final local paths. Both operation types preflight free
space, remove failed partials, support cancellation, and detect workers interrupted by MCP process
exit. Interrupted operations are marked failed rather than resumed because Jenkins responses are not
assumed to support byte-range recovery.

## HTTP Reliability

Ordinary JSON/XML responses are consumed incrementally and rejected as soon as their configured
byte limit is exceeded. Transient GET transport failures and HTTP 429/502/503/504 responses are
retried with bounded backoff. POST requests are never generically retried. Transport exceptions are
converted to safe structured MCP errors without including credentials or full external URLs.

## Source of Truth

Endpoint behavior is based on Jenkins 2.579 source under `vendor/jenkins` and official Jenkins documentation. Plugin-dependent behavior is marked explicitly.
