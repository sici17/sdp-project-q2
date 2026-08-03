import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

from retrieval.search import probe_embedding_model, probe_qdrant, search_manual

MCP_PROTOCOL_VERSION = "2025-11-25"

TOOLS = [
    {
        "name": "manual.search",
        "title": "Search machine manuals",
        "description": "Search indexed machine manual chunks and return cited evidence.",
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "machineId": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                "manualVersion": {"type": "string"},
                "language": {"type": "string"},
            },
            "required": ["machineId", "query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    }
]
_readiness_lock = Lock()
_readiness_cache: tuple[float, dict] | None = None


class DocumentMcpHandler(BaseHTTPRequestHandler):
    server_version = "ArolDocumentMCP/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"status": "ok", "service": "doc-mcp"})
            return
        if self.path == "/ready":
            if not self._authorized():
                self._send_json({"error": "MCP authentication failed."}, 401)
                return
            payload = readiness_status()
            self._send_json(payload, 200 if payload["status"] == "ready" else 503)
            return
        if self.path == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.end_headers()
            return
        self._send_json({"error": "Route not found."}, 404)

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self._send_json({"error": "Route not found."}, 404)
            return
        if not self._authorized():
            self._send_json({"error": "MCP authentication failed."}, 401)
            return
        if not _valid_protocol_header(self.headers.get("MCP-Protocol-Version")):
            self._send_json({"error": "Unsupported MCP protocol version."}, 400)
            return

        message = self._read_json()
        if message is None:
            return
        response = dispatch_mcp(message)
        if response is None:
            self.send_response(202)
            self.end_headers()
            return
        self._send_json(response)

    def log_message(self, format: str, *args) -> None:
        return

    def _authorized(self) -> bool:
        secret = os.environ.get("MCP_SHARED_SECRET", "")
        return bool(secret) and compare_digest(
            self.headers.get("X-Arol-Mcp-Secret", ""),
            secret,
        )

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"error": "Invalid Content-Length."}, 400)
            return None
        if length <= 0 or length > 1_048_576:
            self._send_json({"error": "Invalid MCP request size."}, 400)
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "Invalid JSON."}, 400)
            return None
        if not isinstance(payload, dict):
            self._send_json({"error": "JSON-RPC request must be an object."}, 400)
            return None
        return payload

    def _send_json(self, payload: object, status_code: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def dispatch_mcp(message: dict) -> dict | None:
    request_id = message.get("id")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(request_id, -32600, "Invalid Request")

    method = message["method"]
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
        if params.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            return _error(request_id, -32602, "Unsupported MCP protocol version.")
        return _result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "arol-doc-mcp", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _call_tool(request_id, params)
    return _error(request_id, -32601, f"Method not found: {method}")


def readiness_status() -> dict:
    global _readiness_cache

    now = time.monotonic()
    with _readiness_lock:
        if _readiness_cache is not None and _readiness_cache[0] > now:
            return _readiness_cache[1]

        checks = {
            "qdrant": lambda: probe_qdrant(timeout_seconds=0.75),
            "embeddingModel": lambda: probe_embedding_model(timeout_seconds=0.75),
        }
        statuses: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {name: executor.submit(check) for name, check in checks.items()}
            for name, future in futures.items():
                try:
                    future.result(timeout=0.9)
                    statuses[name] = "ready"
                except Exception:
                    statuses[name] = "unavailable"
        ready = all(status == "ready" for status in statuses.values())
        payload = {
            "status": "ready" if ready else "not_ready",
            "service": "doc-mcp",
            "dependencies": statuses,
            "tools": [tool["name"] for tool in TOOLS],
        }
        _readiness_cache = (now + (30 if ready else 3), payload)
        return payload


def _call_tool(request_id: object, params: dict) -> dict:
    if params.get("name") != "manual.search":
        return _error(request_id, -32602, f"Unknown tool: {params.get('name')}")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _result(
            request_id,
            _tool_result({"error": "Tool arguments must be an object."}, is_error=True),
        )
    try:
        machine_id = _required_string(arguments, "machineId")
        query = _required_string(arguments, "query")
        limit = int(arguments.get("limit", 3))
        if not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10.")
        payload = search_manual(
            machine_id,
            query,
            limit,
            manual_version=_optional_string(arguments, "manualVersion"),
            language=_optional_string(arguments, "language"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _result(request_id, _tool_result({"error": str(exc)}, is_error=True))
    except Exception:
        return _result(
            request_id,
            _tool_result({"error": "Manual search dependency failed."}, is_error=True),
        )
    return _result(request_id, _tool_result(payload))


def _required_string(arguments: dict, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required.")
    return value


def _optional_string(arguments: dict, key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string.")
    return value or None


def _tool_result(payload: object, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _valid_protocol_header(value: str | None) -> bool:
    return value in {None, "", MCP_PROTOCOL_VERSION}


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main() -> None:
    port = int(os.environ.get("PORT", "8092"))
    server = ThreadingHTTPServer(("0.0.0.0", port), DocumentMcpHandler)
    print(f"Document MCP listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
