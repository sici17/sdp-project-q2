from typing import Any, Protocol

from arol_ai.config import Settings
from arol_ai.domain.models import (
    OrderRecord,
    QuoteRecord,
    ServiceContract,
    ServiceHistoryRecord,
)
from arol_ai.mcp import HttpMcpTransport, InProcessMcpTransport, McpClient, McpError
from arol_ai.mcp.server import JsonRpcMcpServer


class BusinessConnector(Protocol):
    name: str

    def service_contract(self, machine_id: str) -> ServiceContract | None:
        """Return service entitlement or warranty context for a machine, if available."""
        ...

    def orders(self, machine_id: str) -> list[OrderRecord]:
        """Return business orders associated with a machine."""
        ...

    def service_history(self, machine_id: str) -> list[ServiceHistoryRecord]:
        """Return completed and scheduled service records for a machine."""
        ...

    def quotes(self, company_id: str) -> list[QuoteRecord]:
        """Return quotations issued to a company, with their current revision."""
        ...

    def quote_comparison(self, quote_id: str) -> dict | None:
        """Return how the latest revision of a quotation changed."""
        ...


class BusinessConnectorError(RuntimeError):
    """Raised when the configured business data source cannot be reached."""


class UnavailableBusinessConnector:
    name = "business.unavailable"

    def service_contract(self, machine_id: str) -> ServiceContract | None:
        _ = machine_id
        return None

    def orders(self, machine_id: str) -> list[OrderRecord]:
        _ = machine_id
        return []

    def service_history(self, machine_id: str) -> list[ServiceHistoryRecord]:
        _ = machine_id
        return []

    def quotes(self, company_id: str) -> list[QuoteRecord]:
        # Quotations come from the fleet dataset through Business MCP.
        return []

    def quote_comparison(self, quote_id: str) -> dict | None:
        return None


class McpBusinessConnector:
    """Business connector whose only data-access boundary is an MCP client."""

    name = "business.mcp"

    def __init__(self, client: McpClient) -> None:
        self.client = client

    def service_contract(self, machine_id: str) -> ServiceContract | None:
        payload = self._call_tool(
            "business.get_service_entitlement",
            {"machineId": machine_id},
        )
        return ServiceContract.from_dict(payload) if isinstance(payload, dict) else None

    def orders(self, machine_id: str) -> list[OrderRecord]:
        payload = self._call_tool("business.list_orders", {"machineId": machine_id})
        if not isinstance(payload, list):
            return []
        return [OrderRecord.from_dict(item) for item in payload if isinstance(item, dict)]

    def service_history(self, machine_id: str) -> list[ServiceHistoryRecord]:
        payload = self._call_tool(
            "business.list_service_history",
            {"machineId": machine_id},
        )
        if not isinstance(payload, list):
            return []
        return [ServiceHistoryRecord.from_dict(item) for item in payload if isinstance(item, dict)]

    def quotes(self, company_id: str) -> list[QuoteRecord]:
        payload = self._call_tool("business.list_quotes", {"companyId": company_id})
        if not isinstance(payload, list):
            return []
        return [QuoteRecord.from_dict(item) for item in payload if isinstance(item, dict)]

    def quote_comparison(self, quote_id: str) -> dict | None:
        payload = self._call_tool("business.compare_quote_revisions", {"quoteId": quote_id})
        return payload if isinstance(payload, dict) else None

    def _call_tool(self, name: str, arguments: dict) -> Any:
        try:
            return self.client.call_tool(name, arguments)
        except (McpError, TimeoutError, OSError) as exc:
            raise BusinessConnectorError("Business MCP is unavailable.") from exc


def build_business_connector(settings: Settings) -> BusinessConnector:
    if settings.business_mcp_url:
        return McpBusinessConnector(
            McpClient(
                HttpMcpTransport(
                    settings.business_mcp_url,
                    shared_secret=settings.mcp_shared_secret,
                ),
                client_name="arol-ai-business-agent",
            )
        )

    # Without an MCP URL there is no business source. The unavailable connector
    # says so, rather than inventing records.
    server = _local_business_mcp_server(UnavailableBusinessConnector())
    return McpBusinessConnector(
        McpClient(
            InProcessMcpTransport(server.dispatch),
            client_name="arol-ai-business-agent",
        )
    )


def _local_business_mcp_server(source: BusinessConnector) -> JsonRpcMcpServer:
    return JsonRpcMcpServer(
        name="arol-business-mcp",
        version="0.1.0",
        tools=BUSINESS_MCP_TOOLS,
        handlers={
            "business.get_service_entitlement": lambda arguments: _optional_dict(
                source.service_contract(_machine_id(arguments))
            ),
            "business.list_orders": lambda arguments: [
                item.to_dict() for item in source.orders(_machine_id(arguments))
            ],
            "business.list_service_history": lambda arguments: [
                item.to_dict() for item in source.service_history(_machine_id(arguments))
            ],
        },
    )


def _machine_id(arguments: dict) -> str:
    machine_id = arguments.get("machineId")
    if not isinstance(machine_id, str) or not machine_id.strip():
        raise ValueError("machineId is required.")
    return machine_id


def _optional_dict(contract: ServiceContract | None) -> dict | None:
    return contract.to_dict() if contract else None


BUSINESS_MCP_TOOLS = [
    {
        "name": "business.get_service_entitlement",
        "title": "Get service entitlement",
        "description": "Read warranty, SLA, maintenance contract, and service due data.",
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
        "name": "business.list_orders",
        "title": "List machine orders",
        "description": "List service and spare-parts orders for a machine.",
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
        "name": "business.list_service_history",
        "title": "List service history",
        "description": "List completed and scheduled service records for a machine.",
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"machineId": {"type": "string", "minLength": 1}},
            "required": ["machineId"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
]
