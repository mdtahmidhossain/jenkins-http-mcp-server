from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import ConfigError


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean value")


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _float_env(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class JenkinsConfig:
    url: str
    user: str | None
    api_token: str | None
    verify_ssl: bool = True
    timeout_seconds: float = 30.0
    max_response_bytes: int = 2_000_000
    max_log_bytes: int = 200_000
    enable_writes: bool = False
    enable_job_config_write: bool = False
    enable_delete: bool = False

    @classmethod
    def from_env(cls) -> JenkinsConfig:
        url = os.getenv("JENKINS_URL", "").strip()
        if not url:
            raise ConfigError("JENKINS_URL is required")

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError("JENKINS_URL must be an absolute http(s) URL")

        user = os.getenv("JENKINS_USER") or None
        api_token = os.getenv("JENKINS_API_TOKEN") or None
        if bool(user) ^ bool(api_token):
            raise ConfigError("JENKINS_USER and JENKINS_API_TOKEN must be set together")

        return cls(
            url=url.rstrip("/") + "/",
            user=user,
            api_token=api_token,
            verify_ssl=_bool_env("JENKINS_VERIFY_SSL", True),
            timeout_seconds=_float_env("JENKINS_TIMEOUT_SECONDS", 30.0),
            max_response_bytes=_int_env("JENKINS_MCP_MAX_RESPONSE_BYTES", 2_000_000),
            max_log_bytes=_int_env("JENKINS_MCP_MAX_LOG_BYTES", 200_000),
            enable_writes=_bool_env("JENKINS_MCP_ENABLE_WRITES", False),
            enable_job_config_write=_bool_env("JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE", False),
            enable_delete=_bool_env("JENKINS_MCP_ENABLE_DELETE", False),
        )

    def require_writes(self) -> None:
        from .errors import PermissionGateError

        if not self.enable_writes:
            raise PermissionGateError("Write tools require JENKINS_MCP_ENABLE_WRITES=1")

    def require_job_config_write(self) -> None:
        from .errors import PermissionGateError

        self.require_writes()
        if not self.enable_job_config_write:
            raise PermissionGateError(
                "Job config write tools require JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE=1"
            )

    def require_delete(self) -> None:
        from .errors import PermissionGateError

        self.require_job_config_write()
        if not self.enable_delete:
            raise PermissionGateError("Delete tools require JENKINS_MCP_ENABLE_DELETE=1")
