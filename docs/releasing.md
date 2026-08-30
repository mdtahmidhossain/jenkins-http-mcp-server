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
  --description "External Python MCP server for Jenkins 2.579 using normal HTTP APIs, with safe local downloads and gated writes." \
  --add-topic jenkins \
  --add-topic mcp \
  --add-topic python \
  --add-topic codex \
  --add-topic gemini-cli \
  --add-topic ci-cd
```
