import json
from collections.abc import Callable
from typing import Any

from arol_ai.mcp.client import MCP_PROTOCOL_VERSION

ToolHandler = Callable[[dict[str, Any]], Any]


class JsonRpcMcpServer:
    """Stateless MCP server core shared by in-process deterministic adapters."""

    def __init__(
        self, *, name: str, version: str, tools: list[dict], handlers: dict[str, ToolHandler]
    ):
        self.name = name
        self.version = version
        self.tools = tools
        self.handlers = handlers

    def dispatch(self, message: dict) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(
                message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request"
            )

        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return None
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Params must be an object.")

        if method == "notifications/initialized":
            return None
        if method == "initialize":
            requested = params.get("protocolVersion")
            if requested != MCP_PROTOCOL_VERSION:
                return _error(request_id, -32602, "Unsupported MCP protocol version.")
            return _result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            )
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": self.tools})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return _error(request_id, -32601, f"Method not found: {method}")

    def _call_tool(self, request_id: object, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        handler = self.handlers.get(name)
        if handler is None:
            return _error(request_id, -32602, f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "Tool arguments must be an object.")

        try:
            structured = handler(arguments)
        except (KeyError, TypeError, ValueError) as exc:
            return _result(request_id, _tool_result({"error": str(exc)}, is_error=True))
        except Exception:
            return _result(
                request_id,
                _tool_result({"error": "Tool execution failed."}, is_error=True),
            )
        return _result(request_id, _tool_result(structured))


def _tool_result(structured: Any, *, is_error: bool = False) -> dict:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=True, separators=(",", ":")),
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
    }


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
