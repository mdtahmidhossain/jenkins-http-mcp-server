# Releasing

The distribution name is `jenkins-http-mcp-server`. The shorter
`jenkins-mcp-server` name is owned by an unrelated PyPI project; this was verified through PyPI's
JSON API on 2026-08-30.

## One-Time Setup

1. Create the `jenkins-http-mcp-server` project on PyPI, or configure a pending publisher.
2. Add a PyPI trusted publisher for GitHub repository
   `mdtahmidhossain/jenkins-http-mcp-server`, workflow `release.yml`, environment `pypi`.
3. Create a protected GitHub environment named `pypi` and require review if appropriate.

The workflow uses OIDC trusted publishing and stores no PyPI API token.

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
python -m twine check dist/*
```

4. Commit the release, then create and push an annotated matching tag:

```bash
git tag -a v0.2.0 -m "Release 0.2.0"
git push origin main
git push origin v0.2.0
```

The release workflow verifies that the tag, project version, and module version match. It runs
lint, compilation, and the full test suite before building and checking both distributions,
publishes through the `pypi` environment, and creates a GitHub release only after PyPI succeeds.

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
