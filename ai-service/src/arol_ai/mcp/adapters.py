from arol_ai.config import Settings
from arol_ai.mcp.client import HttpMcpTransport, InProcessMcpTransport, McpClient
from arol_ai.mcp.server import JsonRpcMcpServer
from arol_ai.rag.retriever import ManualVectorRetriever

MANUAL_SEARCH_TOOL = {
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


def build_manual_mcp_client(
    settings: Settings,
    *,
    retriever: ManualVectorRetriever | None,
) -> McpClient | None:
    if not settings.doc_rag_enabled:
        return None
    if settings.doc_mcp_url:
        return McpClient(
            HttpMcpTransport(
                settings.doc_mcp_url,
                shared_secret=settings.mcp_shared_secret,
            ),
            client_name="arol-ai-manual-agent",
        )
    if retriever is None:
        return None

    server = JsonRpcMcpServer(
        name="arol-doc-mcp-in-process",
        version="0.1.0",
        tools=[MANUAL_SEARCH_TOOL],
        handlers={"manual.search": lambda arguments: _manual_search(retriever, arguments)},
    )
    return McpClient(
        InProcessMcpTransport(server.dispatch),
        client_name="arol-ai-manual-agent",
    )


def _manual_search(retriever: ManualVectorRetriever, arguments: dict) -> list[dict]:
    machine_id = _required_string(arguments, "machineId")
    query = _required_string(arguments, "query")
    limit = int(arguments.get("limit", 3))
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10.")
    evidence = retriever.search(
        machine_id=machine_id,
        query=query,
        manual_version=_optional_string(arguments, "manualVersion"),
        language=_optional_string(arguments, "language"),
        limit=limit,
    )
    return [item.to_dict() for item in evidence]


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
