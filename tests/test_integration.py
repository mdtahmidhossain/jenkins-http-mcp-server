from __future__ import annotations

import os

import pytest

from jenkins_mcp_server.client import JenkinsClient

integration_enabled = (
    os.getenv("JENKINS_INTEGRATION_TESTS") == "1"
    and os.getenv("JENKINS_URL")
    and os.getenv("JENKINS_USER")
    and os.getenv("JENKINS_API_TOKEN")
)


@pytest.mark.skipif(not integration_enabled, reason="Jenkins integration tests are disabled")
def test_integration_whoami() -> None:
    with JenkinsClient.from_env() as client:
        payload = client.get_json("whoAmI")

    assert "authenticated" in payload
