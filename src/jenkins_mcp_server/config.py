from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
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
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _path_env(name: str) -> Path | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path.resolve()


def _ensure_directory(path: Path, env_name: str) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"{env_name} could not be created or accessed") from exc
    if not path.is_dir():
        raise ConfigError(f"{env_name} must identify a directory")
    return path


@dataclass(frozen=True)
class JenkinsConfig:
    url: str
    user: str | None
    api_token: str | None
    verify_ssl: bool = True
    timeout_seconds: float = 30.0
    max_response_bytes: int = 2_000_000
    max_log_bytes: int = 200_000
    max_log_scan_bytes: int = 1_200_000_000
    enable_writes: bool = False
    enable_job_config_write: bool = False
    enable_delete: bool = False
    enable_workspace_download: bool = False
    workspace_download_dir: Path | None = None
    max_workspace_archive_bytes: int = 6_000_000_000
    max_workspace_extract_bytes: int = 20_000_000_000
    max_workspace_files: int = 200_000
    max_bundle_log_bytes: int = 1_200_000_000
    workspace_progress_interval_seconds: float = 2.0
    enable_artifact_download: bool = False
    artifact_download_dir: Path | None = None
    max_artifact_bytes: int = 6_000_000_000
    artifact_progress_interval_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> JenkinsConfig:
        url = os.getenv("JENKINS_URL", "").strip()
        if not url:
            raise ConfigError("JENKINS_URL is required")

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError as exc:
            raise ConfigError("JENKINS_URL must be a valid absolute http(s) URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or hostname is None
            or any(character.isspace() or ord(character) < 32 for character in url)
        ):
            raise ConfigError("JENKINS_URL must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError("JENKINS_URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ConfigError("JENKINS_URL must not contain a query string or fragment")

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
            max_log_scan_bytes=_int_env(
                "JENKINS_MCP_MAX_LOG_SCAN_BYTES",
                1_200_000_000,
            ),
            enable_writes=_bool_env("JENKINS_MCP_ENABLE_WRITES", False),
            enable_job_config_write=_bool_env("JENKINS_MCP_ENABLE_JOB_CONFIG_WRITE", False),
            enable_delete=_bool_env("JENKINS_MCP_ENABLE_DELETE", False),
            enable_workspace_download=_bool_env(
                "JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD",
                False,
            ),
            workspace_download_dir=_path_env("JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR"),
            max_workspace_archive_bytes=_int_env(
                "JENKINS_MCP_MAX_WORKSPACE_ARCHIVE_BYTES",
                6_000_000_000,
            ),
            max_workspace_extract_bytes=_int_env(
                "JENKINS_MCP_MAX_WORKSPACE_EXTRACT_BYTES",
                20_000_000_000,
            ),
            max_workspace_files=_int_env("JENKINS_MCP_MAX_WORKSPACE_FILES", 200_000),
            max_bundle_log_bytes=_int_env(
                "JENKINS_MCP_MAX_BUNDLE_LOG_BYTES",
                1_200_000_000,
            ),
            workspace_progress_interval_seconds=_float_env(
                "JENKINS_MCP_WORKSPACE_PROGRESS_INTERVAL_SECONDS",
                2.0,
            ),
            enable_artifact_download=_bool_env(
                "JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD",
                False,
            ),
            artifact_download_dir=_path_env("JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR"),
            max_artifact_bytes=_int_env(
                "JENKINS_MCP_MAX_ARTIFACT_BYTES",
                6_000_000_000,
            ),
            artifact_progress_interval_seconds=_float_env(
                "JENKINS_MCP_ARTIFACT_PROGRESS_INTERVAL_SECONDS",
                2.0,
            ),
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

    def require_workspace_download(self) -> Path:
        from .errors import PermissionGateError

        if not self.enable_workspace_download:
            raise PermissionGateError(
                "Workspace bundle tools require JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1"
            )
        if self.workspace_download_dir is None:
            raise PermissionGateError(
                "Workspace bundle tools require JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR"
            )
        return _ensure_directory(
            self.workspace_download_dir,
            "JENKINS_MCP_WORKSPACE_DOWNLOAD_DIR",
        )

    def require_artifact_download(self) -> Path:
        from .errors import PermissionGateError

        if not self.enable_artifact_download:
            raise PermissionGateError(
                "Artifact download tools require JENKINS_MCP_ENABLE_ARTIFACT_DOWNLOAD=1"
            )
        if self.artifact_download_dir is None:
            raise PermissionGateError(
                "Artifact download tools require JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR"
            )
        return _ensure_directory(
            self.artifact_download_dir,
            "JENKINS_MCP_ARTIFACT_DOWNLOAD_DIR",
        )
