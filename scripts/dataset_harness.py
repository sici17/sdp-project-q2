"""Run the real orchestrator against the real dataset with nothing running.

The Compose acceptance run is the headline measurement: it reaches Qdrant,
Ollama, the telemetry service and Business MCP over the network, exactly as the
platform does in the demo. This harness exists for the other half of the
job — a regression net that runs in seconds on a laptop and in CI, on the same
scenarios and the same data.

What is real here and what is not is worth stating plainly, because a benchmark
that overstates its own scope is the thing this milestone is meant to fix.

**Real.** The LangGraph orchestrator, the routing, the guardrails, the access
model, the answer composition. The eight supplied manual PDFs, searched by the
lexical retriever that ships with the service. The fleet dataset, read through
the same SQL the MCP servers run in production, mapped to domain models by the
same connectors, over the same MCP client and server plumbing.

**Not real.** The network. There is no HTTP hop, no separate server process, no
vector index and no embedding model: retrieval here is lexical, where the demo
is hybrid lexical and vector. Latency measured here is a regression baseline for
the graph, not a claim about the deployed system.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
AI_SRC = ROOT / "ai-service" / "src"
if str(AI_SRC) not in sys.path:
    sys.path.insert(0, str(AI_SRC))

from arol_ai.config import Settings, get_settings  # noqa: E402
from arol_ai.connectors.business import (  # noqa: E402
    McpBusinessConnector,
    UnavailableBusinessConnector,
)
from arol_ai.connectors.telemetry import (  # noqa: E402
    McpTelemetryConnector,
    UnavailableTelemetryConnector,
)
from arol_ai.data.manifest_repository import ManifestMachineRepository  # noqa: E402
from arol_ai.graph.orchestrator import Orchestrator  # noqa: E402
from arol_ai.mcp import InProcessMcpTransport, McpClient  # noqa: E402
from arol_ai.mcp.server import JsonRpcMcpServer  # noqa: E402
from arol_ai.rag.lexical import ManualLexicalRetriever  # noqa: E402
from arol_ai.tools.business_tools import (  # noqa: E402
    OrderHistoryTool,
    QuoteHistoryTool,
    ServiceEntitlementTool,
    ServiceHistoryTool,
)
from arol_ai.tools.doc_tools import ManualSearchTool  # noqa: E402
from arol_ai.tools.registry import ToolRegistry  # noqa: E402
from arol_ai.tools.telemetry_tools import (  # noqa: E402
    AlarmCodeLookupTool,
    TelemetrySnapshotTool,
)
from arol_ai.tools.troubleshooting_tools import TroubleshootingTool  # noqa: E402

DATASET = ROOT / "data" / "arol_q2.sqlite"
MANUALS_DIR = ROOT / "requirements" / "manuals"
BUSINESS_QUERIES = ROOT / "mcp-servers" / "business-mcp" / "queries.py"
TELEMETRY_DATASET = ROOT / "mcp-servers" / "telemetry-mcp" / "dataset.py"


def _load_module(path: Path, name: str) -> ModuleType:
    """Import an MCP server's query module by path, under its own name.

    Both servers keep a ``queries.py``, so they are loaded under distinct names
    rather than by adding their directories to ``sys.path``, where the second
    import would silently return the first server's module.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def available_capabilities() -> frozenset[str]:
    """What this machine can actually supply, checked rather than assumed."""
    capabilities = set()
    if MANUALS_DIR.is_dir() and any(MANUALS_DIR.glob("*.pdf")):
        capabilities.add("manuals")
    if DATASET.is_file():
        capabilities.update({"telemetry", "business"})
    return frozenset(capabilities)


# ---------------------------------------------------------------------------
# Business: the production SQL, the production connector, no HTTP
# ---------------------------------------------------------------------------

_BUSINESS_TOOL_NAMES = (
    "business.get_service_entitlement",
    "business.list_orders",
    "business.list_service_history",
    "business.list_quotes",
    "business.compare_quote_revisions",
)


def _business_tools() -> list[dict]:
    def schema(field: str) -> dict:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {field: {"type": "string", "minLength": 1}},
            "required": [field],
            "additionalProperties": False,
        }

    fields = {
        "business.list_quotes": "companyId",
        "business.compare_quote_revisions": "quoteId",
    }
    return [
        {
            "name": name,
            "title": name,
            "description": f"Offline dataset handler for {name}.",
            "inputSchema": schema(fields.get(name, "machineId")),
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        }
        for name in _BUSINESS_TOOL_NAMES
    ]


def build_business_connector():
    if not DATASET.is_file():
        return UnavailableBusinessConnector()

    queries = _load_module(BUSINESS_QUERIES, "arol_business_mcp_queries")
    server = JsonRpcMcpServer(
        name="arol-business-mcp-offline",
        version="0.1.0",
        tools=_business_tools(),
        handlers={
            "business.get_service_entitlement": lambda args: queries.get_service_entitlement(
                args["machineId"]
            ),
            "business.list_orders": lambda args: queries.list_orders(
                machine_id=args["machineId"]
            ),
            "business.list_service_history": lambda args: queries.list_service_history(
                args["machineId"]
            ),
            "business.list_quotes": lambda args: queries.list_quotes(args["companyId"]),
            "business.compare_quote_revisions": lambda args: queries.compare_quote_revisions(
                args["quoteId"]
            ),
        },
    )
    return McpBusinessConnector(
        McpClient(
            InProcessMcpTransport(server.dispatch),
            client_name="arol-evaluation-business",
        )
    )


# ---------------------------------------------------------------------------
# Telemetry: the dataset stream, shaped by the production connector
# ---------------------------------------------------------------------------

_TELEMETRY_TOOL_NAMES = (
    "telemetry.latest_snapshot",
    "telemetry.history",
    "telemetry.active_alarms",
    "telemetry.alarm_frequency",
    "telemetry.alarm_lookup",
)


def build_telemetry_mcp_client():
    """A client for the offline telemetry MCP, for tools that call it directly.

    ``telemetry.alarm_lookup`` resolves an ALnnn code against the machine's own
    recorded alarm history. Without it the evaluation cannot answer an alarm
    question at all once the manual declines to supply a passage, which is the
    normal case for this corpus: the codes are the dataset's, and the manuals
    never print them.

    ``telemetry.history`` feeds the maintenance-due estimate. Without it that
    answer can only say the status could not be calculated - a failure the
    running stack, whose server has the tool, does not have.
    """
    if not DATASET.is_file():
        return None

    return McpClient(
        InProcessMcpTransport(_telemetry_server().dispatch),
        client_name="arol-evaluation-telemetry",
    )


def _telemetry_server() -> JsonRpcMcpServer:
    dataset = _load_module(TELEMETRY_DATASET, "arol_telemetry_mcp_dataset")
    return JsonRpcMcpServer(
        name="arol-telemetry-mcp-offline",
        version="0.1.0",
        tools=[
            {
                "name": name,
                "title": name,
                "description": f"Offline dataset handler for {name}.",
                "inputSchema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "machineId": {"type": "string", "minLength": 1},
                        "code": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1440},
                    },
                    "required": ["machineId"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            }
            for name in _TELEMETRY_TOOL_NAMES
        ],
        handlers={
            "telemetry.latest_snapshot": lambda args: dataset.latest(args["machineId"]),
            "telemetry.history": lambda args: dataset.history(
                args["machineId"], args.get("limit", 60)
            ),
            "telemetry.active_alarms": lambda args: dataset.alarms(args["machineId"]),
            "telemetry.alarm_frequency": lambda args: dataset.alarm_frequency(
                args["machineId"]
            ),
            "telemetry.alarm_lookup": lambda args: dataset.alarm_lookup(
                args["machineId"], args["code"]
            ),
        },
    )


def build_telemetry_connector(settings: Settings):
    if not DATASET.is_file():
        return UnavailableTelemetryConnector()

    return McpTelemetryConnector(
        client=McpClient(
            InProcessMcpTransport(_telemetry_server().dispatch),
            client_name="arol-evaluation-telemetry",
        ),
        stale_after_seconds=settings.telemetry_stale_seconds,
    )


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def build_orchestrator(settings: Settings | None = None) -> Orchestrator:
    active_settings = settings or get_settings()
    retriever = ManualLexicalRetriever(manuals_dir=MANUALS_DIR) if MANUALS_DIR.is_dir() else None

    repository = ManifestMachineRepository(
        active_settings,
        telemetry_connector=build_telemetry_connector(active_settings),
        # The manifest is generated from the dataset and already carries the
        # serial, model, plant and owning company.
        business_connector=build_business_connector(),
    )

    return Orchestrator(
        repository=repository,
        tools=ToolRegistry(
            manual_search=ManualSearchTool(
                retriever=retriever,
                rag_enabled=retriever is not None,
            ),
            telemetry_snapshot=TelemetrySnapshotTool(),
            alarm_lookup=AlarmCodeLookupTool(mcp_client=build_telemetry_mcp_client()),
            troubleshooting=TroubleshootingTool(),
            service_entitlement=ServiceEntitlementTool(),
            order_history=OrderHistoryTool(),
            quote_history=QuoteHistoryTool(),
            service_history=ServiceHistoryTool(),
        ),
    )
