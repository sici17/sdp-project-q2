from arol_ai.mcp.client import MCP_PROTOCOL_VERSION, InProcessMcpTransport, McpClient
from arol_ai.mcp.server import JsonRpcMcpServer


def _server() -> JsonRpcMcpServer:
    return JsonRpcMcpServer(
        name="test-mcp",
        version="1.0.0",
        tools=[
            {
                "name": "test.echo",
                "description": "Echo one value.",
                "inputSchema": {"type": "object"},
            }
        ],
        handlers={"test.echo": lambda arguments: {"value": arguments.get("value")}},
    )


def test_mcp_client_negotiates_lifecycle_and_calls_tool() -> None:
    client = McpClient(InProcessMcpTransport(_server().dispatch))

    assert client.initialize()["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert [tool["name"] for tool in client.list_tools()] == ["test.echo"]
    assert client.call_tool("test.echo", {"value": "safe"}) == {"value": "safe"}


def test_mcp_dispatch_rejects_non_object_params_but_suppresses_notifications() -> None:
    server = _server()

    invalid_request = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": [],
        }
    )
    malformed_notification = server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": [],
        }
    )

    assert invalid_request == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "Params must be an object."},
    }
    assert malformed_notification is None
