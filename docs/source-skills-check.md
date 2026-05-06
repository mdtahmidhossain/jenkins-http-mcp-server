# Jenkins Source Skills Check

Date checked: 2026-05-06

Commands were run inside `vendor/jenkins` at tag `jenkins-2.563`.

## Commands and Results

`find . -name SKILL.md`

Result: no output.

`find . -name AGENTS.md`

Result: no output.

`find . \( -path '*skills*' -o -path '*Skills*' \)`

Result: no output.

`rg -i 'agent skill|SKILL.md|\.agents|\.gemini|codex|gemini cli|mcp'`

Result: no relevant agent skills, Codex, Gemini CLI, or MCP instructions were found. Matches were unrelated Jenkins agent package/class names and test fixture text, for example:

- `core/src/main/java/jenkins/agents/...`
- `test/src/test/java/jenkins/agents/...`
- `test/src/test/java/jenkins/install/SetupWizardTest.java` fixture text containing `mcp` as part of encoded data

## Conclusion

Jenkins 2.563 source did not contain relevant `SKILL.md`, `AGENTS.md`, `.agents`, `.gemini`, Codex, Gemini CLI, or MCP agent-skill guidance. New project-local skills were created under `.agents/skills/`.
