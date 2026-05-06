from __future__ import annotations

import pytest

from jenkins_mcp_server.config import JenkinsConfig
from jenkins_mcp_server.errors import ConfigError, PermissionGateError


def test_config_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_VERIFY_SSL", "false")
    monkeypatch.setenv("JENKINS_TIMEOUT_SECONDS", "10.5")
    monkeypatch.setenv("JENKINS_MCP_MAX_RESPONSE_BYTES", "1234")
    monkeypatch.setenv("JENKINS_MCP_MAX_LOG_BYTES", "456")

    config = JenkinsConfig.from_env()

    assert config.url == "https://jenkins.example.com/"
    assert config.user == "alice"
    assert config.api_token == "secret"
    assert config.verify_ssl is False
    assert config.timeout_seconds == 10.5
    assert config.max_response_bytes == 1234
    assert config.max_log_bytes == 456


def test_workspace_download_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD", "1")
    monkeypatch.setenv("JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("JENKINS_MCP_MAX_WORKSPACE_ARCHIVE_BYTES", "6000000000")
    monkeypatch.setenv("JENKINS_MCP_MAX_WORKSPACE_EXTRACT_BYTES", "20000000000")
    monkeypatch.setenv("JENKINS_MCP_MAX_WORKSPACE_FILES", "200000")
    monkeypatch.setenv("JENKINS_MCP_MAX_BUNDLE_LOG_BYTES", "1200000000")

    config = JenkinsConfig.from_env()

    assert config.enable_workspace_download is True
    assert config.require_workspace_download() == (tmp_path / "bundles").resolve()
    assert config.max_workspace_archive_bytes == 6_000_000_000
    assert config.max_workspace_extract_bytes == 20_000_000_000
    assert config.max_workspace_files == 200_000
    assert config.max_bundle_log_bytes == 1_200_000_000


def test_config_requires_user_and_token_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.delenv("JENKINS_API_TOKEN", raising=False)

    with pytest.raises(ConfigError):
        JenkinsConfig.from_env()


def test_write_gates_block_by_default() -> None:
    config = JenkinsConfig(url="https://jenkins.example.com/", user="u", api_token="t")

    with pytest.raises(PermissionGateError):
        config.require_writes()
    with pytest.raises(PermissionGateError):
        config.require_job_config_write()
    with pytest.raises(PermissionGateError):
        config.require_delete()
    with pytest.raises(PermissionGateError):
        config.require_workspace_download()


def test_dangerous_delete_requires_separate_flag() -> None:
    config = JenkinsConfig(
        url="https://jenkins.example.com/",
        user="u",
        api_token="t",
        enable_writes=True,
        enable_job_config_write=True,
        enable_delete=False,
    )

    with pytest.raises(PermissionGateError):
        config.require_delete()
