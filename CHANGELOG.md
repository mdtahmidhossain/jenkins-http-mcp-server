# Changelog

All notable changes are documented here.

## 0.2.0 - Unreleased

- Add progressive build-log chunks and bounded literal log search.
- Enforce response limits while streaming and return structured transport failures.
- Retry transient GET failures without replaying POST requests.
- Add gated asynchronous artifact downloads with progress and cancellation.
- Add disk-space preflight, interrupted-operation recovery, and explicit workspace retention cleanup.
- Coordinate workspace captures across MCP processes with detached workers, guarded Jenkins REST
  state, terminal-result reuse, and build-number-grouped local output.
- Reject unsafe Jenkins URLs, non-finite numeric settings, relative download roots, encoded generic
  path traversal, malformed crumb responses, and invalid version/health responses.
- Reserve local download outputs atomically with owner-only permissions and prevent cancellation from
  rewriting terminal progress.
- Refresh Python 3.14.7, Codex environment forwarding, Gemini token forwarding, and Agent Skills
  documentation against current official sources.
- Depend on the MCP SDK without its unused CLI extra.
- Add MIT licensing, security policy, and package metadata.
- Prevent HTTP content coding from wrapping file downloads and invalidating byte accounting.

## 0.1.0 - 2026-05-06

- Initial external Jenkins HTTP MCP server with read-only defaults, gated writes, workspace bundle
  downloads, Codex/Gemini setup documentation, and Jenkins source evidence.
