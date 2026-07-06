"""Model Context Protocol client and in-process transport boundaries."""

from arol_ai.mcp.client import (
    MCP_PROTOCOL_VERSION,
    HttpMcpTransport,
    InProcessMcpTransport,
    McpClient,
    McpError,
    McpToolError,
)

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "HttpMcpTransport",
    "InProcessMcpTransport",
    "McpClient",
    "McpError",
    "McpToolError",
]
