from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class JenkinsMCPError(Exception):
    """Base error with a structured payload safe to return through MCP."""

    code = "jenkins_mcp_error"

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self)}}


class ConfigError(JenkinsMCPError):
    code = "config_error"


class PermissionGateError(JenkinsMCPError):
    code = "permission_gate"


class PathValidationError(JenkinsMCPError):
    code = "invalid_jenkins_path"


class ToolInputError(JenkinsMCPError):
    code = "invalid_tool_input"


class ResponseTooLargeError(JenkinsMCPError):
    code = "response_too_large"

    def __init__(self, limit: int) -> None:
        super().__init__(f"Jenkins response exceeded configured limit of {limit} bytes")
        self.limit = limit

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["error"]["limit"] = self.limit
        return data


class OperationCancelledError(JenkinsMCPError):
    code = "operation_cancelled"


class JenkinsProtocolError(JenkinsMCPError):
    code = "jenkins_protocol_error"


class WorkspaceListingError(JenkinsMCPError):
    code = "workspace_listing_unavailable"


@dataclass
class JenkinsTransportError(JenkinsMCPError):
    kind: str
    method: str
    path: str
    attempts: int

    @property
    def code(self) -> str:  # type: ignore[override]
        return {
            "timeout": "jenkins_timeout",
            "tls": "jenkins_tls_error",
            "connection": "jenkins_connection_error",
        }.get(self.kind, "jenkins_transport_error")

    def __str__(self) -> str:
        messages = {
            "timeout": "Jenkins request timed out",
            "tls": "Jenkins TLS validation failed",
            "connection": "Could not connect to Jenkins",
        }
        message = messages.get(self.kind, "Jenkins transport failed")
        return f"{message}: {self.method} {self.path} after {self.attempts} attempt(s)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": str(self),
                "method": self.method,
                "path": self.path,
                "attempts": self.attempts,
            },
        }


@dataclass
class InsufficientDiskSpaceError(JenkinsMCPError):
    required_bytes: int
    available_bytes: int
    path: str

    code = "insufficient_disk_space"

    def __str__(self) -> str:
        return (
            f"Insufficient free disk space under {self.path}: "
            f"need {self.required_bytes} bytes, have {self.available_bytes} bytes"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": str(self),
                "required_bytes": self.required_bytes,
                "available_bytes": self.available_bytes,
                "path": self.path,
            },
        }


class WorkspaceBundleError(JenkinsMCPError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self._code = code

    @property
    def code(self) -> str:  # type: ignore[override]
        return self._code


@dataclass
class JenkinsHTTPError(JenkinsMCPError):
    status_code: int
    method: str
    path: str
    message: str
    body: str | None = None

    @property
    def code(self) -> str:  # type: ignore[override]
        if self.status_code == 401:
            return "jenkins_unauthorized"
        if self.status_code == 403:
            return "jenkins_forbidden"
        if self.status_code == 404:
            return "jenkins_not_found"
        if self.status_code in {400, 405, 409, 422}:
            return "jenkins_request_rejected"
        return "jenkins_http_error"

    def __str__(self) -> str:
        hint = ""
        if self.status_code == 401:
            hint = " Check JENKINS_USER and JENKINS_API_TOKEN."
        elif self.status_code == 403:
            hint = (
                " Jenkins denied access; credentials may be invalid, your user may lack the"
                " required permission, or a crumb may be required."
            )
        elif self.status_code == 404:
            hint = " The endpoint, job, build, plugin-provided action, or item was not found."
        body = f" Body: {self.body}" if self.body else ""
        return (
            f"{self.method} {self.path} failed with HTTP {self.status_code}: "
            f"{self.message}.{hint}{body}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": str(self),
                "status_code": self.status_code,
                "method": self.method,
                "path": self.path,
                "body": self.body,
            },
        }
