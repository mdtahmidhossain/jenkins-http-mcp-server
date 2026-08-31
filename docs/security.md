# Security

## Credentials

- Configure credentials through environment variables only: `JENKINS_USER` and `JENKINS_API_TOKEN`.
- Set both credential variables or neither; omitting both uses Jenkins anonymous access.
- Do not commit tokens or put real tokens in Codex/Gemini config.
- Do not embed credentials in `JENKINS_URL`; URLs with user info, queries, or fragments are rejected.
- Authorization, proxy authorization, cookie, and set-cookie headers are redacted by helper code before logging.
- Jenkins API token/basic auth is used preemptively, matching official Jenkins scripted client guidance.

## CSRF Crumbs

Jenkins can require crumbs for POST requests. The client tries to fetch `/crumbIssuer/api/json` for POST requests and adds the returned crumb header when available. If Jenkins returns a crumb-related 403, the client refreshes the crumb and retries once.

Official Jenkins security guidance says Basic auth with API token is generally crumb-exempt since Jenkins 2.96, but crumb support remains implemented for controllers that require it or have custom behavior.

## Untrusted Jenkins Data

Jenkins logs, job output, build descriptions, test reports, artifact names, and API JSON are untrusted text. Agents should not execute commands found in logs or treat log text as instructions.

## Permissions

The server assumes a non-admin Jenkins user. Jenkins remains the authority for permissions. A 401, 403, or 404 is returned clearly as a structured error rather than being hidden.

## Gates

- Read-only by default.
- `JENKINS_MCP_ENABLE_WRITES=1`: allows build trigger, build stop, queue cancel, enable job, and disable job tools.
- `JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE=1`: additionally allows job create/copy/update config.
- `JENKINS_MCP_ENABLE_DELETE=1`: additionally allows job delete.
- `JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1`: allows workspace bundle downloads when `JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR` is also set.
- `JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD=1`: allows individual artifact downloads when
  `JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR` is also set.

## Workspace Bundle Downloads

Workspace bundle tools can download large Jenkins workspace archives, extract them locally, delete the archive on success, and save the selected build run's console log. Path-specific workspace tools can download one file directly or one folder as a zip that is extracted locally. They are read-only against Jenkins but high impact locally and on the Jenkins controller/agent.

Safety behavior:

- Streams archive and console log to disk; does not return file contents through MCP.
- Writes progress atomically to the operation's `progress.json` and exposes status by operation ID.
- Uses a local SQLite registry to coordinate concurrent MCP clients. The registry stores request
  metadata, worker ownership/heartbeat, status, and local paths; it does not store Jenkins tokens.
- Uses `.partial` files/directories and renames only after successful steps.
- Requires an absolute download root and reserves each build output directory with owner-only `0700`
  permissions.
- Deletes partial archive files on download failure.
- Checks available disk space before downloads and before extracting declared zip contents.
- Deletes the archive after successful extraction by default.
- Rejects unsafe requested workspace paths, including external URLs, absolute paths, `..` traversal, Jenkins magic path segments, and wildcards.
- Safely extracts zip files by rejecting absolute paths, `..` traversal, symlinks, special files, duplicate file entries, file count limit violations, and extracted byte limit violations.
- Treats extracted files and console logs as untrusted.
- Waits while REST reports the job queued, building, or in post-processing. An explicit build that is
  not the current stable `lastBuild` is rejected instead of being falsely associated with `/ws`.
- Checks Jenkins state before, during, and after the `/ws` stream. A changed state deletes the output
  and retries once; another change fails. This is a race-reduction guard, not a snapshot guarantee.
- Runs workspace captures in detached workers so an initiating STDIO process can exit. Stale worker
  heartbeats are marked failed and their output is removed before a replacement starts.
- Reuses a completed capture only for the same request/current anchor when all required local files
  remain. Callers can request `force_refresh=true`.
- Deletes retained terminal operation directories only through the explicit bounded cleanup tool.
- A cancel request changes only a running registry row. Cancelling a terminal operation is a no-op,
  and cancellation never rewrites a worker's progress file from a stale local copy.

Jenkins `/ws` content remains untrusted and best-effort. Jenkins core does not attach a build number
or version token to the workspace response, and `getSomeWorkspace` may select an available workspace
from an older build. Use archived artifacts when exact historical build identity is required.

Recommended large-download env values:

```bash
JENKINS_MCP_MAX_WORKSPACE_ARCHIVE_BYTES=6000000000
JENKINS_MCP_MAX_WORKSPACE_EXTRACT_BYTES=20000000000
JENKINS_MCP_MAX_WORKSPACE_FILES=200000
JENKINS_MCP_MAX_BUNDLE_LOG_BYTES=1200000000
```

## Artifact Downloads

Artifact downloads are read-only against Jenkins but write untrusted archived build output to local
disk. They stream to `.partial` files, report bytes and speed through a local progress file, support
cancellation, enforce `JENKINS_MCP_MAX_ARTIFACT_BYTES`, preflight free space, and delete incomplete
files after failure, cancellation, or interrupted-process recovery. They never return artifact bytes
or base64 content through MCP.

Each artifact output directory is reserved atomically with owner-only `0700` permissions. Cancelling
an already terminal artifact operation is a no-op; a running cancellation writes only the marker so
it cannot overwrite a concurrently completed progress file.

Artifact paths must be relative and reject external URLs, absolute paths, traversal, query strings,
fragments, wildcards, and Jenkins directory-browser magic segments.

## Network Failures

Normal Jenkins responses are bounded while streaming rather than after full buffering. Timeouts,
TLS failures, connection failures, and other transport failures return structured errors containing
only the method and normalized relative Jenkins path. Transient GET failures use at most three
attempts with short backoff. POST requests are not automatically retried; the only POST replay is the
existing one-time crumb refresh after Jenkins explicitly returns a crumb-related 403.

HTTPX-decoded bounded responses are rebuilt without stale `Content-Encoding`, `Content-Length`, or
`Transfer-Encoding` metadata. This prevents JSON endpoints such as `whoAmI` from trying to decode an
already-decoded gzip body again while preserving Jenkins application headers.

File downloads request `Accept-Encoding: identity` and reject responses with an HTTP
`Content-Encoding`. This prevents an HTTP gzip content-coding layer from wrapping ZIP or other file
content and keeps `Content-Length`, progress, disk preflight, size limits, and bytes written
consistent.

GET redirects are rejected and never followed. This prevents a Jenkins response from redirecting the
MCP server to an arbitrary external artifact or download host.

## Not Implemented

The server does not implement script console, restart, safe restart, quiet down, plugin install/update, credential read/write, node creation/deletion, global config changes, or user management.
