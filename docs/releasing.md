# Releasing

Releases are GitHub Releases only. This project does not publish packages to PyPI.

## Release

1. Replace `Unreleased` with the date in `CHANGELOG.md`.
2. Set the same version in `pyproject.toml` and
   `src/jenkins_mcp_server/__init__.py`.
3. Run:

```bash
python -m pytest
python -m compileall src
ruff check
python -m pip install -e '.[dev,release]'
rm -rf build dist
python -m build
```

4. Commit the release, then create and push an annotated matching tag:

```bash
git tag -a v0.2.0 -m "Release 0.2.0"
git push origin main
git push origin v0.2.0
```

The release workflow verifies that the tag, project version, and module version match. It runs
lint, compilation, and the full test suite before building both distributions and attaching them to
a GitHub release. It does not publish to PyPI.

## Repository Metadata

After authenticating GitHub CLI, set the public description and topics once:

```bash
gh auth login
gh repo edit mdtahmidhossain/jenkins-http-mcp-server \
  --description "External Python Model Context Protocol (MCP) server for Jenkins HTTP APIs. Read-only by default; supports Codex CLI and Gemini CLI without Jenkins admin access or plugins." \
  --add-topic jenkins \
  --add-topic jenkins-api \
  --add-topic jenkins-mcp \
  --add-topic mcp \
  --add-topic mcp-server \
  --add-topic model-context-protocol \
  --add-topic python \
  --add-topic devops \
  --add-topic continuous-integration \
  --add-topic build-automation \
  --add-topic codex \
  --add-topic codex-cli \
  --add-topic gemini-cli \
  --add-topic ci-cd \
  --add-topic agent-skills \
  --add-topic stdio
```

The tracked social-preview source is `.github/assets/social-preview.png`. After changing it, upload
that file under the repository's **Settings > Social preview** control. GitHub does not expose a
supported social-preview upload through its REST or GraphQL repository update APIs.
