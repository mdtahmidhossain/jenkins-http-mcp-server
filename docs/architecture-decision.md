# Architecture Decision

## Decision

Use option B only: an external Python MCP server that connects to Jenkins through normal Jenkins HTTP APIs available to `JENKINS_USER` and `JENKINS_API_TOKEN`.

## Rationale

- The user does not have Jenkins administrator permission.
- The server must not require Jenkins plugin installation.
- Jenkins 2.563 core and official docs expose the needed HTTP endpoints through normal Remote Access API and Stapler web methods.
- The official Jenkins MCP Server Plugin is not usable here because it requires installation/enabling inside Jenkins.

## Transport

STDIO is implemented first using the official MCP Python SDK `FastMCP`.

HTTP transport was not added because STDIO is the requested first target for Codex CLI and Gemini CLI. The SDK supports streamable HTTP, so it can be added later as a small launcher option if needed.

## Safety Model

- Default mode is read-only.
- Write tools require `JENKINS_MCP_ENABLE_WRITES=1`.
- Job config write tools require `JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE=1`.
- Delete requires `JENKINS_MCP_ENABLE_DELETE=1`.
- Admin-like operations are not implemented: script console, restart, safe restart, quiet down, plugin install/update, credential read/write, node creation/deletion, global config changes, and user management.

## Source of Truth

Endpoint behavior is based on Jenkins 2.563 source under `vendor/jenkins` and official Jenkins documentation. Plugin-dependent behavior is marked explicitly.
