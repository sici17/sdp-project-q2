import json
from collections.abc import Callable
from threading import Lock
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MCP_PROTOCOL_VERSION = "2025-11-25"


class McpError(RuntimeError):
    """Raised when an MCP peer returns an invalid response or protocol error."""


class McpToolError(McpError):
    """Raised when an MCP tool reports an execution error."""


class McpTransport(Protocol):
    def send(self, message: dict, *, protocol_version: str | None = None) -> dict | None:
        """Send one JSON-RPC message and return its response, if one is expected."""
        ...


class HttpMcpTransport:
    """Stateless MCP Streamable HTTP transport for request/response tool servers."""

    def __init__(
        self,
        endpoint: str,
        *,
        shared_secret: str | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        normalized = endpoint.rstrip("/")
        self.endpoint = normalized if normalized.endswith("/mcp") else f"{normalized}/mcp"
        self.shared_secret = shared_secret
        self.timeout_seconds = timeout_seconds

    def send(self, message: dict, *, protocol_version: str | None = None) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if protocol_version:
            headers["MCP-Protocol-Version"] = protocol_version
        if self.shared_secret:
            headers["X-Arol-Mcp-Secret"] = self.shared_secret

        request = Request(
            self.endpoint,
            data=json.dumps(message, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise McpError(f"MCP HTTP request failed with {exc.code}: {detail}") from exc

        if not body:
            return None

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpError("MCP server returned invalid JSON.") from exc

        if not isinstance(payload, dict):
            raise McpError("MCP server returned a non-object JSON-RPC response.")
        return payload


class InProcessMcpTransport:
    """JSON-RPC transport used for deterministic local mode and unit tests."""

    def __init__(self, dispatcher: Callable[[dict], dict | None]) -> None:
        self.dispatcher = dispatcher

    def send(self, message: dict, *, protocol_version: str | None = None) -> dict | None:
        _ = protocol_version
        return self.dispatcher(message)


class McpClient:
    """Small MCP client implementing lifecycle negotiation and tool calls."""

    def __init__(
        self,
        transport: McpTransport,
        *,
        client_name: str = "arol-ai-service",
        client_version: str = "0.1.0",
    ) -> None:
        self.transport = transport
        self.client_name = client_name
        self.client_version = client_version
        self.protocol_version: str | None = None
        self._next_request_id = 1
        self._lock = Lock()

    def initialize(self) -> dict:
        with self._lock:
            if self.protocol_version:
                return {"protocolVersion": self.protocol_version}

            result = self._request_unlocked(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self.client_name,
                        "version": self.client_version,
                    },
                },
                include_protocol_header=False,
            )
            negotiated = result.get("protocolVersion")
            if negotiated != MCP_PROTOCOL_VERSION:
                raise McpError(
                    "MCP protocol negotiation failed: "
                    f"server selected unsupported version {negotiated!r}."
                )
            self.protocol_version = negotiated
            self.transport.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                protocol_version=self.protocol_version,
            )
            return result

    def list_tools(self) -> list[dict]:
        self.initialize()
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpError("MCP tools/list response did not contain a tools array.")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.initialize()
        result = self._request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments or {},
            },
        )
        if result.get("isError"):
            raise McpToolError(_tool_error_message(result))
        if "structuredContent" in result:
            return result["structuredContent"]

        content = result.get("content")
        if not isinstance(content, list):
            raise McpError("MCP tool response contained no structuredContent or content.")
        text = next(
            (
                item.get("text")
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ),
            None,
        )
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _request(self, method: str, params: dict) -> dict:
        with self._lock:
            return self._request_unlocked(method, params, include_protocol_header=True)

    def _request_unlocked(
        self,
        method: str,
        params: dict,
        *,
        include_protocol_header: bool,
    ) -> dict:
        request_id = self._next_request_id
        self._next_request_id += 1
        response = self.transport.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
            protocol_version=self.protocol_version if include_protocol_header else None,
        )
        if not isinstance(response, dict):
            raise McpError(f"MCP request {method!r} returned no JSON-RPC response.")
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise McpError(f"MCP request {method!r} returned an invalid JSON-RPC envelope.")
        if "error" in response:
            error = response["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise McpError(f"MCP request {method!r} failed: {detail}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpError(f"MCP request {method!r} returned an invalid result.")
        return result


def _tool_error_message(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ):
                return item["text"]
    return "MCP tool execution failed."
