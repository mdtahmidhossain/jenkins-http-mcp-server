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
