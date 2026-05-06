from __future__ import annotations

from jenkins_mcp_server.auth import REDACTED, redact_headers


def test_auth_header_redaction() -> None:
    headers = redact_headers(
        {
            "Authorization": "Basic abc123",
            "X-Test": "visible",
            "cookie": "JSESSIONID=secret",
        }
    )

    assert headers["Authorization"] == REDACTED
    assert headers["cookie"] == REDACTED
    assert headers["X-Test"] == "visible"
