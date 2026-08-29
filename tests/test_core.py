from __future__ import annotations

import logging
import runpy
import sys
from collections.abc import Callable

import httpx
import pytest
from mcp.server import MCPServer

from jenkins_mcp_server.crumbs import CrumbManager
from jenkins_mcp_server.errors import (
    JenkinsHTTPError,
    JenkinsMCPError,
    ResponseTooLargeError,
    WorkspaceBundleError,
)
from jenkins_mcp_server.logging import get_logger, safe_headers_for_log
from jenkins_mcp_server.resources import register_resources


def test_module_entrypoint_runs_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    transports: list[str] = []

    def fake_run(self: MCPServer, *, transport: str) -> None:
        transports.append(transport)

    monkeypatch.setattr(MCPServer, "run", fake_run)
    monkeypatch.delitem(sys.modules, "jenkins_mcp_server.__main__", raising=False)

    runpy.run_module("jenkins_mcp_server.__main__", run_name="__main__")

    assert transports == ["stdio"]


def test_crumb_manager_caches_valid_crumb() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"},
        )

    manager = CrumbManager()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = manager.get(client, "https://jenkins.example.com/")
        second = manager.get(client, "https://jenkins.example.com/")

    assert first == second
    assert calls == 1


def test_crumb_manager_ignores_incomplete_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert CrumbManager().get(client, "https://jenkins.example.com/") is None


def test_structured_base_and_size_errors() -> None:
    assert JenkinsMCPError("failed").to_dict() == {
        "ok": False,
        "error": {"code": "jenkins_mcp_error", "message": "failed"},
    }

    error = ResponseTooLargeError(12)
    assert error.to_dict()["error"] == {
        "code": "response_too_large",
        "message": "Jenkins response exceeded configured limit of 12 bytes",
        "limit": 12,
    }

    workspace_error = WorkspaceBundleError("workspace_failed", "failed")
    assert workspace_error.code == "workspace_failed"


@pytest.mark.parametrize(
    ("status", "code", "hint"),
    [
        (401, "jenkins_unauthorized", "Check JENKINS_USER"),
        (403, "jenkins_forbidden", "Jenkins denied access"),
        (404, "jenkins_not_found", "was not found"),
        (400, "jenkins_request_rejected", ""),
        (500, "jenkins_http_error", ""),
    ],
)
def test_http_error_codes_messages_and_payloads(status: int, code: str, hint: str) -> None:
    error = JenkinsHTTPError(status, "GET", "api/json", "Failure", "details")

    assert error.code == code
    if hint:
        assert hint in str(error)
    payload = error.to_dict()["error"]
    assert payload["code"] == code
    assert payload["status_code"] == status
    assert payload["method"] == "GET"
    assert payload["path"] == "api/json"
    assert payload["body"] == "details"


def test_logging_helpers_redact_sensitive_headers() -> None:
    assert get_logger("jenkins.test") is logging.getLogger("jenkins.test")
    assert safe_headers_for_log(
        {"Proxy-Authorization": "secret", "Set-Cookie": "secret", "X-Test": "visible"}
    ) == {
        "Proxy-Authorization": "<redacted>",
        "Set-Cookie": "<redacted>",
        "X-Test": "visible",
    }


def test_safety_resource_returns_operational_guards() -> None:
    resources: dict[str, Callable[[], str]] = {}

    class ResourceRecorder:
        def resource(self, uri: str):
            def register(fn: Callable[[], str]) -> Callable[[], str]:
                resources[uri] = fn
                return fn

            return register

    register_resources(ResourceRecorder())  # type: ignore[arg-type]

    safety = resources["jenkins-mcp://safety"]()
    assert "read-only by default" in safety
    assert "JENKINS_MCP_ENABLE_WORKSPACE_DOWNLOAD=1" in safety
    assert "untrusted text" in safety
