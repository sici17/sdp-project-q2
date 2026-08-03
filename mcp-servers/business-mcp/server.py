import json
import os
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from queries import (
    compare_quote_revisions,
    get_quote,
    get_service_entitlement,
    list_maintenance_tickets,
    list_orders,
    list_quotes,
    list_service_history,
    machine_ids,
    machine_purchase_summary,
)

MCP_PROTOCOL_VERSION = "2025-11-25"

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_READ_ONLY = {"readOnlyHint": True, "idempotentHint": True}


def _machine_schema() -> dict:
    return {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {"machineId": {"type": "string", "minLength": 1}},
        "required": ["machineId"],
        "additionalProperties": False,
    }


def _company_schema() -> dict:
    return {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {"companyId": {"type": "string", "minLength": 1}},
        "required": ["companyId"],
        "additionalProperties": False,
    }


def _quote_schema() -> dict:
    return {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {"quoteId": {"type": "string", "minLength": 1}},
        "required": ["quoteId"],
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "business.list_quotes",
        "title": "List company quotes",
        "description": (
            "List quotations issued to a company. Each entry carries its current "
            "revision, since a quote has no status of its own."
        ),
        "inputSchema": _company_schema(),
        "annotations": _READ_ONLY,
    },
    {
        "name": "business.get_quote",
        "title": "Get quote with revision history",
        "description": (
            "Read one quotation with every revision and the lines of each "
            "revision. Line prices are already net of the revision discount."
        ),
        "inputSchema": _quote_schema(),
        "annotations": _READ_ONLY,
    },
    {
        "name": "business.compare_quote_revisions",
        "title": "Compare the two latest quote revisions",
        "description": "Explain how the most recent revision of a quotation changed.",
        "inputSchema": _quote_schema(),
        "annotations": _READ_ONLY,
    },
    {
        "name": "business.list_orders",
        "title": "List orders",
        "description": (
            "List confirmed orders for a machine or a company, with content taken "
            "from the approved quote revision and fulfilment from the order lines."
        ),
        "inputSchema": {
            "$schema": _SCHEMA,
            "type": "object",
            "properties": {
                "machineId": {"type": "string", "minLength": 1},
                "companyId": {"type": "string", "minLength": 1},
            },
            "anyOf": [{"required": ["machineId"]}, {"required": ["companyId"]}],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "business.machine_purchase_summary",
        "title": "Get machine delivery and cost",
        "description": "Read delivery date, configuration and acquisition cost for a machine.",
        "inputSchema": _machine_schema(),
        "annotations": _READ_ONLY,
    },
    {
        "name": "business.list_maintenance_tickets",
        "title": "List maintenance tickets",
        "description": (
            "List service and maintenance activities for a machine or company, "
            "joined to the originating alarm where the ticket came from one."
        ),
        "inputSchema": {
            "$schema": _SCHEMA,
            "type": "object",
            "properties": {
                "machineId": {"type": "string", "minLength": 1},
                "companyId": {"type": "string", "minLength": 1},
            },
            "anyOf": [{"required": ["machineId"]}, {"required": ["companyId"]}],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "business.get_service_entitlement",
        "title": "Get service standing",
        "description": (
            "Read delivery, acquisition cost and maintenance standing for a "
            "machine. The dataset holds no warranty or SLA records."
        ),
        "inputSchema": _machine_schema(),
        "annotations": _READ_ONLY,
    },
    {
        "name": "business.list_service_history",
        "title": "List service history",
        "description": "List the maintenance ticket record for a machine.",
        "inputSchema": _machine_schema(),
        "annotations": _READ_ONLY,
    },
]


class BusinessMcpHandler(BaseHTTPRequestHandler):
    server_version = "ArolBusinessMCP/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(
                {
                    "status": "ok",
                    "service": "business-mcp",
                    "machineCount": len(machine_ids()),
                }
            )
            return
        if self.path == "/ready":
            if not self._authorized():
                self._send_json({"error": "MCP authentication failed."}, 401)
                return
            try:
                count = len(machine_ids())
            except Exception:
                self._send_json(
                    {
                        "status": "not_ready",
                        "service": "business-mcp",
                        "tools": [tool["name"] for tool in TOOLS],
                    },
                    503,
                )
                return
            self._send_json(
                {
                    "status": "ready",
                    "service": "business-mcp",
                    "machineCount": count,
                    "tools": [tool["name"] for tool in TOOLS],
                }
            )
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
                "serverInfo": {"name": "arol-business-mcp", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
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
    if name not in {tool["name"] for tool in TOOLS}:
        return _error(request_id, -32602, f"Unknown tool: {name}")

    try:
        payload = _run_tool(name, arguments)
    except (KeyError, TypeError, ValueError) as exc:
        return _result(request_id, _tool_result({"error": str(exc)}, is_error=True))
    except FileNotFoundError as exc:
        return _result(request_id, _tool_result({"error": str(exc)}, is_error=True))
    return _result(request_id, _tool_result(payload))


def _identifier(arguments: dict, key: str, *, required: bool = True) -> str | None:
    value = arguments.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required.")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _run_tool(name: str, arguments: dict) -> object:
    if name == "business.list_quotes":
        return list_quotes(_identifier(arguments, "companyId"))
    if name == "business.get_quote":
        return get_quote(_identifier(arguments, "quoteId"))
    if name == "business.compare_quote_revisions":
        return compare_quote_revisions(_identifier(arguments, "quoteId"))
    if name in {"business.list_orders", "business.list_maintenance_tickets"}:
        machine_id = _identifier(arguments, "machineId", required=False)
        company_id = _identifier(arguments, "companyId", required=False)
        if not machine_id and not company_id:
            raise ValueError("Either machineId or companyId is required.")
        if name == "business.list_orders":
            return list_orders(machine_id=machine_id, company_id=company_id)
        return list_maintenance_tickets(machine_id=machine_id, company_id=company_id)

    machine_id = _identifier(arguments, "machineId")
    if name == "business.machine_purchase_summary":
        return machine_purchase_summary(machine_id)
    if name == "business.get_service_entitlement":
        return get_service_entitlement(machine_id)
    return list_service_history(machine_id)


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
    port = int(os.environ.get("PORT", "8091"))
    server = ThreadingHTTPServer(("0.0.0.0", port), BusinessMcpHandler)
    print(f"Business MCP listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
