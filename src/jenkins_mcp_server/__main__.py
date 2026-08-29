from __future__ import annotations

from mcp.server import MCPServer

from .resources import register_resources
from .tools import register_tools


def build_server() -> MCPServer:
    mcp = MCPServer(
        "jenkins-mcp-server",
        instructions=(
            "External Jenkins MCP server using normal Jenkins HTTP APIs. "
            "Read-only by default; write tools are gated by environment flags."
        ),
    )
    register_tools(mcp)
    register_resources(mcp)
    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
