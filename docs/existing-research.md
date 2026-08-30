# Existing Research

Original survey date: 2026-05-06.

Core Jenkins, MCP SDK, client setup, packaging, and listed-project availability were refreshed on
2026-08-30.

Web was used only to verify current public docs and examples. No third-party code was copied.

| Name | URL | Official vs third-party | Relevance | Decision | Date checked |
| --- | --- | --- | --- | --- | --- |
| Jenkins Remote Access API | https://www.jenkins.io/doc/book/using/remote-access-api/ | Official Jenkins docs | Confirms `.../api/`, JSON API, build trigger endpoints, nested job URL example, depth/tree behavior, and `X-Jenkins` version header. | Referenced | 2026-08-30 |
| Jenkins Authenticating scripted clients | https://www.jenkins.io/doc/book/system-administration/authenticating-scripted-clients/ | Official Jenkins docs | Confirms Basic auth with username and API token, preemptive auth behavior, and example crumb usage for scripted clients. | Referenced | 2026-08-30 |
| Jenkins CSRF Protection | https://www.jenkins.io/doc/book/security/csrf-protection/ | Official Jenkins docs | Confirms current crumb concepts, API-token exemption, and removal of the old proxy-compatibility option in Jenkins 2.543. | Referenced | 2026-08-30 |
| Jenkins API security recommendations | https://www.jenkins.io/doc/developer/security/misc/ | Official Jenkins docs | Confirms API-token Basic auth requests generally do not need CSRF crumbs since Jenkins 2.96. | Referenced | 2026-08-30 |
| Jenkins JUnit plugin | https://plugins.jenkins.io/junit | Official Jenkins plugin site | Confirms test reports are plugin-provided and JUnit was split from core. | Referenced; test reports are marked plugin-dependent | 2026-08-30 |
| Jenkins 2.579 changelog | https://www.jenkins.io/changelog/2.579/ | Official Jenkins docs | Confirms Apache Commons Lang 2 was removed from core and installed plugins should be updated before upgrading Jenkins. | Referenced | 2026-08-30 |
| Jenkins security advisory 2026-06-10 | https://www.jenkins.io/security/advisory/2026-06-10/ | Official Jenkins docs | Confirms the 2.568 job `config.xml` serialization and queue permission fixes included in 2.579. | Referenced | 2026-08-30 |
| Jenkins security advisory 2026-08-05 | https://www.jenkins.io/security/advisory/2026-08-05/ | Official Jenkins docs | Confirms security fixes included in Jenkins 2.576 and therefore 2.579. | Referenced; no MCP route change required | 2026-08-30 |
| MCP Python SDK | https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.1 | Official MCP SDK | Confirms v2.1.1 uses `MCPServer`, supports stdio/HTTP, and declares Python 3.14 support. | Reused as dependency | 2026-08-30 |
| MCP Python SDK migration guide | https://py.sdk.modelcontextprotocol.io/migration/ | Official MCP SDK docs | Confirms v2 renamed `FastMCP` to `MCPServer` and removed `mcp.server.fastmcp`. | Referenced | 2026-08-30 |
| PyPI package metadata for `mcp` | https://pypi.org/pypi/mcp/json | Official package metadata via PyPI | Confirms latest `mcp` is `2.1.1` with `Requires-Python: >=3.10`, including Python 3.14. | Referenced | 2026-08-30 |
| PyPI Trusted Publishers | https://docs.pypi.org/trusted-publishers/ | Official PyPI docs | Confirms tokenless OIDC publishing from GitHub Actions and trusted publisher setup. | Referenced | 2026-08-30 |
| PyPA PyPI publish action | https://github.com/pypa/gh-action-pypi-publish | Official PyPA action | Confirms a separate `id-token: write` publish job and no username/password for trusted publishing; latest verified release was `v1.14.2`. | Reused and commit-pinned | 2026-08-30 |
| PyPI `jenkins-mcp-server` metadata | https://pypi.org/pypi/jenkins-mcp-server/json | Official PyPI metadata for a third-party package | Confirms the name is occupied by unrelated version 0.1.6. Its linked GitHub source currently returns 404. | Referenced for package-name collision only | 2026-08-30 |
| OpenAI Codex MCP docs | https://developers.openai.com/codex/mcp/ | Official OpenAI docs | Confirms STDIO config, `env_vars` forwarding, and `codex mcp add`. | Referenced | 2026-08-30 |
| OpenAI Codex CLI local help | `codex mcp --help`, `codex mcp add --help` | Installed official CLI help | Confirmed stdio command syntax and `--env KEY=VALUE` in Codex CLI 0.151.0. | Referenced | 2026-08-30 |
| OpenAI Codex Agent Skills docs | https://developers.openai.com/codex/skills/ | Official OpenAI docs | Confirms `SKILL.md`, YAML metadata, repo `.agents/skills`, and symlink discovery. | Referenced | 2026-08-30 |
| Gemini CLI MCP docs | https://geminicli.com/docs/tools/mcp-server/ | Official Gemini CLI docs | Confirms `mcpServers`, runtime env expansion, sensitive inherited-env sanitization, and explicit token forwarding. | Referenced | 2026-08-30 |
| Gemini CLI local help | `gemini mcp --help`, `gemini mcp add --help`, `gemini skills --help` | Installed official CLI help | Confirmed `--env`, scopes, transports, and skill commands in Gemini CLI 0.55.1. | Referenced | 2026-08-30 |
| Gemini CLI Agent Skills docs | https://geminicli.com/docs/cli/using-agent-skills/ | Official Gemini CLI docs | Confirms both `.gemini/skills` and `.agents/skills` workspace discovery aliases. | Referenced | 2026-08-30 |
| Jenkins MCP Server Plugin | https://github.com/jenkinsci/mcp-server-plugin | Official Jenkins plugin | In-Jenkins MCP implementation, but requires plugin installation/admin ability. | Ignored as unavailable | 2026-08-30 |
| LokiMCPUniverse Jenkins MCP server | https://github.com/LokiMCPUniverse/jenkins-mcp-server | Third-party | Existing external Jenkins MCP server example. | Reviewed at high level only; no code copied | 2026-08-30 |
| lanbaoshen mcp-jenkins | https://github.com/lanbaoshen/mcp-jenkins | Third-party | Existing Jenkins MCP integration. | Reviewed at high level only; no code copied | 2026-08-30 |
| PulseMCP Jenkins MCP Server listing | https://www.pulsemcp.com/servers/jenkins-mcp-server | Third-party listing | Community Jenkins MCP server listing; automated refresh returned HTTP 403. | Retained as original survey evidence; not relied on | 2026-08-30 |
| ALMC Jenkins MCP Server listing | https://almc.es/en/mcpserver/development/jenkins-mcp-server | Third-party listing | Mentions a Python Jenkins MCP server using the MCP Python SDK. | Reviewed at high level only | 2026-08-30 |
| LambdaTest agent-skills | https://github.com/LambdaTest/agent-skills | Third-party | Existing Agent Skills collection with CI/CD mentions. | Reviewed at high level only | 2026-08-30 |

## Existing Jenkins MCP Servers or Skills Found

Found existing Jenkins MCP servers:

- Official Jenkins MCP Server Plugin: unavailable for this project because it requires Jenkins plugin installation/admin access.
- `LokiMCPUniverse/jenkins-mcp-server`
- `lanbaoshen/mcp-jenkins`
- PyPI `jenkins-mcp-server` 0.1.6; its linked `akhilthomas236` GitHub source is no longer available
- PulseMCP Jenkins MCP Server listing
- ALMC Jenkins MCP Server listing

No Jenkins 2.579 source-bundled Agent Skills were found. Public searches found general Agent Skills repositories and CI/CD mentions, but no source-of-truth Jenkins 2.579 skill suitable to reuse.
