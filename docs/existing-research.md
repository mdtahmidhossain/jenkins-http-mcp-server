# Existing Research

Date checked: 2026-05-06

Web was used only to verify current public docs and examples. No third-party code was copied.

| Name | URL | Official vs third-party | Relevance | Reused, referenced, or ignored |
| --- | --- | --- | --- | --- |
| Jenkins Remote Access API | https://www.jenkins.io/doc/book/using/remote-access-api/ | Official Jenkins docs | Confirms `.../api/`, JSON API, build trigger endpoints, nested job URL example, depth/tree behavior, and `X-Jenkins` version header. | Referenced |
| Jenkins Authenticating scripted clients | https://www.jenkins.io/doc/book/system-administration/authenticating-scripted-clients/ | Official Jenkins docs | Confirms Basic auth with username and API token, preemptive auth behavior, and example crumb usage for scripted clients. | Referenced |
| Jenkins CSRF Protection | https://www.jenkins.io/doc/book/security/csrf-protection/ | Official Jenkins docs | Confirms crumb concepts and Jenkins 2.222+ applicability; Jenkins 2.563 removes old IP crumb option. | Referenced |
| Jenkins API security recommendations | https://www.jenkins.io/doc/developer/security/misc/ | Official Jenkins docs | Confirms API-token Basic auth requests generally do not need CSRF crumbs since Jenkins 2.96. | Referenced |
| Jenkins JUnit plugin | https://plugins.jenkins.io/junit | Official Jenkins plugin site | Confirms test reports are plugin-provided and JUnit was split from core. | Referenced; `jenkins_get_test_report` is marked plugin-dependent |
| MCP Python SDK | https://github.com/modelcontextprotocol/python-sdk | Official MCP SDK | Confirms official Python SDK, FastMCP, tools/resources, stdio and HTTP transport support. | Reused as dependency |
| PyPI package metadata for `mcp` | local `pip download --no-deps 'mcp[cli]'` metadata | Official package metadata via PyPI | Confirmed `mcp 1.27.0` has `Requires-Python: >=3.10`, compatible with Python 3.14. | Referenced |
| OpenAI Codex config reference | https://developers.openai.com/codex/config-reference | Official OpenAI docs | Used for current Codex configuration source. | Referenced |
| OpenAI Codex CLI local help | `codex mcp --help`, `codex mcp add --help` | Installed official CLI help | Confirmed `codex mcp add <NAME> -- <COMMAND>...` and `--env KEY=VALUE` for stdio servers. | Referenced |
| OpenAI Skills catalog | https://github.com/openai/skills | Official OpenAI repository | Confirms Agent Skills are `SKILL.md` folders of instructions, scripts, and resources. | Referenced |
| Gemini CLI MCP docs | https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html | Official Gemini CLI docs | Confirms `mcpServers` config, stdio command/args/env, HTTP transports, and `gemini mcp add`. | Referenced |
| Gemini CLI local help | `gemini mcp --help`, `gemini mcp add --help`, `gemini skills --help` | Installed official CLI help | Confirmed `gemini mcp add`, `--env`, scopes, transport options, and `gemini skills install/link`. | Referenced |
| Gemini CLI Extensions docs | https://google-gemini.github.io/gemini-cli/docs/extensions/getting-started-extensions.html | Official Gemini CLI docs | Confirms extensions can bundle MCP servers, but this project uses plain MCP config first. | Referenced |
| Jenkins MCP Server Plugin | https://github.com/jenkinsci/mcp-server-plugin | Official Jenkins plugin | In-Jenkins MCP implementation, but requires plugin installation/admin ability. | Ignored as unavailable |
| LokiMCPUniverse Jenkins MCP server | https://github.com/LokiMCPUniverse/jenkins-mcp-server | Third-party | Existing external Jenkins MCP server example. | Reviewed at high level only; no code copied |
| lanbaoshen mcp-jenkins | https://github.com/lanbaoshen/mcp-jenkins | Third-party | Existing Jenkins MCP integration. | Reviewed at high level only; no code copied |
| PulseMCP Jenkins MCP Server listing | https://www.pulsemcp.com/servers/jenkins-mcp-server | Third-party listing | Shows additional community Jenkins MCP server packaging. | Reviewed at high level only |
| ALMC Jenkins MCP Server listing | https://almc.es/en/mcpserver/development/jenkins-mcp-server | Third-party listing | Mentions Python Jenkins MCP server using MCP Python SDK. | Reviewed at high level only |
| LambdaTest agent-skills | https://github.com/LambdaTest/agent-skills | Third-party | Existing Agent Skills collection with CI/CD category mention. | Reviewed at high level only |

## Existing Jenkins MCP Servers or Skills Found

Found existing Jenkins MCP servers:

- Official Jenkins MCP Server Plugin: unavailable for this project because it requires Jenkins plugin installation/admin access.
- `LokiMCPUniverse/jenkins-mcp-server`
- `lanbaoshen/mcp-jenkins`
- PulseMCP Jenkins MCP Server listing
- ALMC Jenkins MCP Server listing

No Jenkins 2.563 source-bundled Agent Skills were found. Public searches found general Agent Skills repositories and CI/CD mentions, but no source-of-truth Jenkins 2.563 skill suitable to reuse.
