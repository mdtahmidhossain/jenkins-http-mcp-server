from __future__ import annotations

from jenkins_mcp_server.__main__ import build_server
from jenkins_mcp_server.tools import (
    OPTIONAL_JOB_CONFIG_TOOLS,
    READ_ONLY_TOOLS,
    WORKSPACE_BUNDLE_TOOLS,
    WRITE_TOOLS,
)


def test_tool_schemas_registered() -> None:
    mcp = build_server()
    registered = set(mcp._tool_manager._tools.keys())  # noqa: SLF001

    assert set(READ_ONLY_TOOLS).issubset(registered)
    assert set(WRITE_TOOLS).issubset(registered)
    assert set(OPTIONAL_JOB_CONFIG_TOOLS).issubset(registered)
    assert set(WORKSPACE_BUNDLE_TOOLS).issubset(registered)


def test_tool_schema_has_parameters() -> None:
    mcp = build_server()
    tool = mcp._tool_manager._tools["jenkins_get_json"]  # noqa: SLF001

    assert "path" in tool.parameters["properties"]
    assert tool.parameters["required"] == ["path"]
