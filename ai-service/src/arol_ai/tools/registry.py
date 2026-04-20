from dataclasses import dataclass, field

from arol_ai.config import get_settings
from arol_ai.mcp import HttpMcpTransport, McpClient
from arol_ai.mcp.adapters import build_manual_mcp_client
from arol_ai.rag.service import build_manual_retriever
from arol_ai.tools.business_tools import (
    OrderHistoryTool,
    QuoteHistoryTool,
    ServiceEntitlementTool,
    ServiceHistoryTool,
)
from arol_ai.tools.doc_tools import ManualSearchTool
from arol_ai.tools.telemetry_tools import AlarmCodeLookupTool, TelemetrySnapshotTool
from arol_ai.tools.troubleshooting_tools import TroubleshootingTool


@dataclass(frozen=True)
class ToolRegistry:
    manual_search: ManualSearchTool
    telemetry_snapshot: TelemetrySnapshotTool
    troubleshooting: TroubleshootingTool
    service_entitlement: ServiceEntitlementTool
    alarm_lookup: AlarmCodeLookupTool = field(default_factory=AlarmCodeLookupTool)
    order_history: OrderHistoryTool = field(default_factory=OrderHistoryTool)
    quote_history: QuoteHistoryTool = field(default_factory=QuoteHistoryTool)
    service_history: ServiceHistoryTool = field(default_factory=ServiceHistoryTool)


def build_tool_registry() -> ToolRegistry:
    settings = get_settings()
    retriever = build_manual_retriever(settings) if settings.doc_rag_enabled else None
    telemetry_mcp_client = (
        McpClient(
            HttpMcpTransport(
                settings.telemetry_service_url,
                shared_secret=settings.mcp_shared_secret,
            ),
            client_name="arol-ai-alarm-agent",
        )
        if settings.telemetry_service_url
        else None
    )

    return ToolRegistry(
        manual_search=ManualSearchTool(
            retriever=retriever,
            rag_enabled=settings.doc_rag_enabled,
            mcp_client=build_manual_mcp_client(settings, retriever=retriever),
        ),
        telemetry_snapshot=TelemetrySnapshotTool(),
        alarm_lookup=AlarmCodeLookupTool(mcp_client=telemetry_mcp_client),
        troubleshooting=TroubleshootingTool(),
        service_entitlement=ServiceEntitlementTool(),
        order_history=OrderHistoryTool(),
        service_history=ServiceHistoryTool(),
    )


def list_tool_capabilities() -> list[dict]:
    return [
        {
            "name": ManualSearchTool.name,
            "agent": ManualSearchTool.agent,
            "description": "Search indexed machine manual chunks and return cited evidence.",
        },
        {
            "name": TelemetrySnapshotTool.name,
            "agent": TelemetrySnapshotTool.agent,
            "description": "Read the latest telemetry snapshot and return diagnostic evidence.",
        },
        {
            "name": AlarmCodeLookupTool.name,
            "agent": AlarmCodeLookupTool.agent,
            "description": "Resolve an exact ALnnn code from the machine's alarm history.",
        },
        {
            "name": TroubleshootingTool.name,
            "agent": TroubleshootingTool.agent,
            "description": "Build ordered troubleshooting steps from citations, telemetry, and safety rules.",
        },
        {
            "name": ServiceEntitlementTool.name,
            "agent": ServiceEntitlementTool.agent,
            "description": "Read warranty, SLA, maintenance contract, and service entitlement context.",
        },
        {
            "name": OrderHistoryTool.name,
            "agent": OrderHistoryTool.agent,
            "description": "Read machine order status and order history.",
        },
        {
            "name": ServiceHistoryTool.name,
            "agent": ServiceHistoryTool.agent,
            "description": "Read completed and scheduled machine service history.",
        },
    ]
