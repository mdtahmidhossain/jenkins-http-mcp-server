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

## Remote Workspace Listing

`jenkins_get_workspace_tree` uses Jenkins core's authenticated, read-only
`job/{name}/ws/{directory}/*plain*` Stapler endpoint. Each response contains only the immediate
children, so the Python server performs a breadth-first walk. It verifies requested subdirectories
through their parent listings and validates every returned name before placing it in another URL.

The walk is bounded by caller-selected depth and entry limits plus the existing
`JENKINS_MCP_MAX_RESPONSE_BYTES` cumulative response budget. It returns paths through MCP and writes
nothing to disk, so it does not require the workspace download gate. The result remains untrusted
and `best_effort`: `/ws` is dynamic, exposes no build token, and core evidence covers
`AbstractProject` rather than every plugin-defined job type.

## Long-Running Downloads

Workspace downloads run asynchronously in detached Python worker processes. A SQLite registry at
`<workspace-download-root>/.operations/workspace-operations.sqlite3` coordinates all local Codex and
Gemini MCP processes. It permits one active operation per Jenkins URL, Jenkins user, and normalized
request; concurrent starts join that operation. The registry contains request metadata and local
paths, never the API token.

The worker polls the normal job and queue REST APIs until the job is not queued and no recent run is
building or in post-production. It then anchors the capture to the stable `lastBuild`. An explicit
different build is rejected because Jenkins `/ws` cannot retrieve an immutable historical workspace.
The worker checks the same REST state before, during, and after the workspace stream. A change deletes
the attempted output and retries once; a second change fails with
`workspace_changed_during_download`.

This guard is deliberately labeled best-effort. Jenkins core `AbstractProject.doWs` calls
`getSomeWorkspace`, which makes only a cursory effort to select some available build workspace and
does not return a workspace build identity. REST observations reduce the race window but do not make
the workspace endpoint transactional.

Progress is written atomically under the operation directory and returned by
`jenkins_get_workspace_bundle_status`, including wait/capture phase, downloaded bytes, total bytes
when Jenkins sends `Content-Length`, speed, elapsed time, and local paths. Cancellation is durable in
SQLite and also uses a local marker checked during wait, download, extraction, and log capture.
Only running rows accept cancellation; terminal operations are left unchanged.

Completed matching captures are reused only when Jenkins is currently stable on the same anchor and
the expected workspace/path, console log, and metadata still exist. `force_refresh=true` bypasses
reuse. File downloads stream directly to disk. Folder/full-workspace downloads use Jenkins zip
support, extract locally, and delete the archive after successful extraction.

Capture output is grouped as `<workspace-download-root>/<job path>/<build number>/`. Archive and
console filenames retain the combined safe job/build prefix, such as `my-job123.zip` and
`my-job123-console.log`. Existing-directory collisions receive a short operation ID suffix. Build
and artifact output directories are reserved atomically with owner-only `0700` permissions.

Individual build artifacts use a separate async operation root and safety gate. Artifact bytes stream
to disk; MCP returns only status, progress, and final local paths. Both operation types preflight free
space, remove failed partials, and support cancellation. Artifact workers still use the legacy
in-process model; workspace workers are detached and use stale-heartbeat recovery. Interrupted
downloads are not resumed because Jenkins responses are not assumed to support byte-range recovery.
Artifact cancellation writes only its marker and never rewrites progress from a stale read.

## HTTP Reliability

Ordinary JSON/XML responses are consumed incrementally and rejected as soon as their configured
byte limit is exceeded. Transient GET transport failures and HTTP 429/502/503/504 responses are
retried with bounded backoff. POST requests are never generically retried. Transport exceptions are
converted to safe structured MCP errors without including credentials or full external URLs.
After HTTPX decodes a bounded response body, the detached in-memory response drops stale
content-coding and framing metadata so JSON endpoints such as `whoAmI` are not decoded a second time.
File downloads explicitly send `Accept-Encoding: identity` and reject HTTP content-coded responses
so transport compression cannot invalidate byte accounting for archives, artifacts, or logs.

## Source of Truth

Endpoint behavior is based on Jenkins 2.579 source under `vendor/jenkins` and official Jenkins documentation. Plugin-dependent behavior is marked explicitly.
