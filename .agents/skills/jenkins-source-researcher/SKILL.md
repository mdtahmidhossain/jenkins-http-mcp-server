---
name: jenkins-source-researcher
description: Use this skill when researching Jenkins 2.563 source for HTTP endpoints, permissions, or MCP server maintenance evidence.
---

# Jenkins Source Researcher

Research Jenkins behavior from the checked-out source, not memory.

## Source

- Use `vendor/jenkins`, checked out at tag `jenkins-2.563`.
- Verify with `git describe --tags --exact-match` and `git rev-parse HEAD` if needed.
- Cite file paths and line numbers in findings.

## Search Patterns

Use `rg` first:

- Stapler API: `getApi`, `new Api`, `@WebMethod`
- POST endpoints: `@RequirePOST`, `@POST`, `doBuild`, `doBuildWithParameters`, `doStop`, `doDoDelete`
- Config: `doConfigDotXml`, `updateByXml`, `createItem`
- Logs/artifacts: `consoleText`, `progressiveText`, `getArtifacts`, `doArtifact`
- Areas: `crumbIssuer`, `queue`, `computer`, `view`, `pluginManager`, `testReport`

## Classify Evidence

- Distinguish Jenkins core from plugin behavior.
- If evidence is in plugin docs or Javadoc, mark the feature plugin-dependent.
- If an endpoint is not proven, mark it unsupported rather than guessing.
- Record permission checks when discoverable, such as `checkPermission`, `hasPermission`, or documented permission names.

## Output

Summaries should include endpoint path, source file and line references, required permission if discoverable, and any unsupported or plugin-dependent caveat.
