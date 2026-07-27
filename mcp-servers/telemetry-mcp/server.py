import json
import os
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from queries import alarm_frequency, alarm_lookup, alarms, history, latest, machine_ids, source_name

MCP_PROTOCOL_VERSION = "2025-11-25"

MCP_TOOLS = [
    {
        "name": "telemetry.latest_snapshot",
        "title": "Get latest telemetry",
        "description": "Read the latest telemetry snapshot for a machine.",
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"machineId": {"type": "string", "minLength": 1}},
            "required": ["machineId"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "telemetry.history",
        "title": "Get telemetry history",
        "description": "Read bounded recent telemetry history for a machine.",
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "machineId": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 60},
            },
            "required": ["machineId"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "telemetry.active_alarms",
        "title": "Get active alarms",
        "description": "Read the active alarm list for a machine.",
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"machineId": {"type": "string", "minLength": 1}},
            "required": ["machineId"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "telemetry.alarm_lookup",
        "title": "Resolve an alarm code",
        "description": (
            "Resolve an exact ALnnn code from a machine's recorded alarm history, "
            "including its full mnemonic, severity, occurrence count and last-seen time."
        ),
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "machineId": {"type": "string", "minLength": 1},
                "code": {
                    "type": "string",
                    "pattern": "^AL[0-9]{3}(?:_[A-Z0-9_]+)?$",
                },
            },
            "required": ["machineId", "code"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "telemetry.alarm_frequency",
        "title": "Rank alarm codes by frequency",
        "description": (
            "Rank the alarm codes a machine raised, most frequent first, with "
            "unresolved counts. Use it to explain repeated alarms before looking "
            "the codes up in that machine's manual."
        ),
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "machineId": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["machineId"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
]


class TelemetryHandler(BaseHTTPRequestHandler):
    server_version = "ArolTelemetryMCP/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._send_json({"status": "ok", "service": "telemetry-mcp"})
            return

        if parsed.path == "/ready":
            if not self._authorized():
                self._send_json({"error": "MCP authentication failed."}, 401)
                return
            self._send_json(
                {
                    "status": "ready",
                    "service": "telemetry-mcp",
                    "machineCount": len(machine_ids()),
                    "source": source_name(),
                    "tools": [tool["name"] for tool in MCP_TOOLS],
                }
            )
            return

        # Telemetry reveals how a customer's line is running, so the read API is
        # authenticated exactly like the MCP endpoint. Only /health is open, and
        # it discloses nothing about the fleet.
        if not self._authorized():
            self._send_json({"error": "MCP authentication failed."}, 401)
            return

        if parsed.path == "/api/v1/machines":
            self._send_json({"machineIds": machine_ids()})
            return

        match = _match_machine_route(parsed.path, "/telemetry/latest")
        if match:
            payload = latest(match)
            self._send_json(payload if payload else {"error": "Machine not found."}, 200 if payload else 404)
            return

        match = _match_machine_route(parsed.path, "/telemetry/history")
        if match:
            query = parse_qs(parsed.query)
            limit = _query_int(query.get("limit", ["60"])[0], default=60, maximum=1440)
            payload = history(match, limit)
            self._send_json(payload if payload else {"error": "Machine not found."}, 200 if payload else 404)
            return

        match = _match_machine_route(parsed.path, "/alarms")
        if match:
            if match not in machine_ids():
                self._send_json({"error": "Machine not found."}, 404)
                return

            query = parse_qs(parsed.query)
            requested_code = query.get("code", [None])[0]
            if requested_code:
                try:
                    payload = alarm_lookup(match, requested_code)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                self._send_json(
                    payload if payload else {"error": "Alarm code not found for this machine."},
                    200 if payload else 404,
                )
                return

            self._send_json(alarms(match))
            return

        self._send_json({"error": "Route not found."}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/mcp":
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
        body = json.dumps(payload, separators=(",", ":")).encode("utf8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _match_machine_route(path: str, suffix: str) -> str | None:
    prefix = "/api/v1/machines/"

    if not path.startswith(prefix) or not path.endswith(suffix):
        return None

    machine_id = path[len(prefix) : -len(suffix)]
    return unquote(machine_id.strip("/"))


def _query_int(value: str, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return default

    return max(1, min(parsed, maximum))


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
                "serverInfo": {"name": "arol-telemetry-mcp", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": MCP_TOOLS})
    if method == "tools/call":
        return _call_tool(request_id, params)
    return _error(request_id, -32601, f"Method not found: {method}")


def _call_tool(request_id: object, params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _result(
            request_id,
            _tool_result({"error": "Tool arguments must be an object."}, is_error=True),
        )
    if name not in {tool["name"] for tool in MCP_TOOLS}:
        return _error(request_id, -32602, f"Unknown tool: {name}")

    try:
        machine_id = arguments["machineId"]
        if not isinstance(machine_id, str) or not machine_id.strip():
            raise ValueError("machineId is required.")
        if machine_id not in machine_ids():
            return _result(
                request_id,
                _tool_result({"error": "Machine not found."}, is_error=True),
            )
        if name == "telemetry.latest_snapshot":
            payload = latest(machine_id)
        elif name == "telemetry.history":
            limit = int(arguments.get("limit", 60))
            if not 1 <= limit <= 1440:
                raise ValueError("limit must be between 1 and 1440.")
            payload = history(machine_id, limit)
        elif name == "telemetry.alarm_lookup":
            code = arguments.get("code")
            if not isinstance(code, str) or not code.strip():
                raise ValueError("code is required.")
            payload = alarm_lookup(machine_id, code)
        elif name == "telemetry.alarm_frequency":
            limit = int(arguments.get("limit", 10))
            if not 1 <= limit <= 50:
                raise ValueError("limit must be between 1 and 50.")
            payload = alarm_frequency(machine_id, limit)
        else:
            payload = alarms(machine_id)
    except (KeyError, TypeError, ValueError) as exc:
        return _result(request_id, _tool_result({"error": str(exc)}, is_error=True))
    return _result(request_id, _tool_result(payload))


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
    port = int(os.environ.get("PORT", "8090"))
    server = ThreadingHTTPServer(("0.0.0.0", port), TelemetryHandler)
    print(f"Telemetry MCP listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
