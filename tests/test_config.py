from __future__ import annotations

import pytest

from jenkins_mcp_server.config import JenkinsConfig, _bool_env, _float_env, _int_env
from jenkins_mcp_server.errors import ConfigError, PermissionGateError


def test_config_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_API_TOKEN", "secret")
    monkeypatch.setenv("JENKINS_VERIFY_SSL", "false")
    monkeypatch.setenv("JENKINS_TIMEOUT_SECONDS", "10.5")
    monkeypatch.setenv("JENKINS_MCP_MAX_RESPONSE_BYTES", "1234")
    monkeypatch.setenv("JENKINS_MCP_MAX_LOG_BYTES", "456")
    monkeypatch.setenv("JENKINS_MCP_MAX_LOG_SCAN_BYTES", "789")

    config = JenkinsConfig.from_env()

    assert config.url == "https://jenkins.example.com/"
    assert config.user == "alice"
    assert config.api_token == "secret"
    assert config.verify_ssl is False
    assert config.timeout_seconds == 10.5
    assert config.max_response_bytes == 1234
    assert config.max_log_bytes == 456
    assert config.max_log_scan_bytes == 789


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


def test_artifact_download_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com/")
    monkeypatch.setenv("JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD", "1")
    monkeypatch.setenv("JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("JENKINS_MCP_MAX_ARTIFACT_BYTES", "123456")
    monkeypatch.setenv("JENKINS_MCP_ARTIFACT_PROGRESS_INTERVAL_SECONDS", "1.5")

    config = JenkinsConfig.from_env()

    assert config.enable_artifact_download is True
    assert config.require_artifact_download() == (tmp_path / "artifacts").resolve()
    assert config.max_artifact_bytes == 123_456
    assert config.artifact_progress_interval_seconds == 1.5


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
    with pytest.raises(PermissionGateError):
        config.require_artifact_download()


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


def test_environment_parser_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BOOL", "maybe")
    with pytest.raises(ConfigError, match="TEST_BOOL must be a boolean value"):
        _bool_env("TEST_BOOL", False)

    monkeypatch.setenv("TEST_INT", "not-an-integer")
    with pytest.raises(ConfigError, match="TEST_INT must be an integer"):
        _int_env("TEST_INT", 1)
    monkeypatch.setenv("TEST_INT", "0")
    with pytest.raises(ConfigError, match="TEST_INT must be >= 1"):
        _int_env("TEST_INT", 1)

    monkeypatch.setenv("TEST_FLOAT", "not-a-number")
    with pytest.raises(ConfigError, match="TEST_FLOAT must be a number"):
        _float_env("TEST_FLOAT", 1.0)
    monkeypatch.setenv("TEST_FLOAT", "0")
    with pytest.raises(ConfigError, match="TEST_FLOAT must be >= 0.1"):
        _float_env("TEST_FLOAT", 1.0)


@pytest.mark.parametrize("url", ["", "ftp://jenkins.example.com", "https:///missing-host"])
def test_config_rejects_missing_or_invalid_url(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    monkeypatch.setenv("JENKINS_URL", url)

    with pytest.raises(ConfigError):
        JenkinsConfig.from_env()


def test_specific_write_and_workspace_directory_gates() -> None:
    config = JenkinsConfig(
        url="https://jenkins.example.com/",
        user="u",
        api_token="t",
        enable_writes=True,
    )
    with pytest.raises(PermissionGateError, match="JOB_CONFIG_WRITE"):
        config.require_job_config_write()

    workspace_config = JenkinsConfig(
        url="https://jenkins.example.com/",
        user="u",
        api_token="t",
        enable_workspace_download=True,
    )
    with pytest.raises(PermissionGateError, match="WORKSPACE_DOWNLOAD_DIR"):
        workspace_config.require_workspace_download()

    artifact_config = JenkinsConfig(
        url="https://jenkins.example.com/",
        user="u",
        api_token="t",
        enable_artifact_download=True,
    )
    with pytest.raises(PermissionGateError, match="ARTIFACT_DOWNLOAD_DIR"):
        artifact_config.require_artifact_download()
