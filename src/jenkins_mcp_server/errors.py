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


class ResponseTooLargeError(JenkinsMCPError):
    code = "response_too_large"

    def __init__(self, limit: int) -> None:
        super().__init__(f"Jenkins response exceeded configured limit of {limit} bytes")
        self.limit = limit

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["error"]["limit"] = self.limit
        return data


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
                " Jenkins denied access; your user may lack the required permission or a crumb"
                " may be required."
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
