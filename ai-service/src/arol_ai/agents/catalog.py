AGENT_CATALOG = [
    {
        "name": "supervisor",
        "description": "Classifies operator intent and routes work to specialized agents.",
        "inputs": ["message", "conversation_history", "machine_id"],
        "outputs": ["agent_trace"],
    },
    {
        "name": "doc-agent",
        "description": "Retrieves manual snippets and troubleshooting procedures with page citations.",
        "inputs": ["message", "manual_documents"],
        "outputs": ["manual_evidence"],
    },
    {
        "name": "telemetry-agent",
        "description": "Summarizes machine health, alarms, and telemetry diagnostics.",
        "inputs": ["machine_id", "telemetry_snapshot"],
        "outputs": ["telemetry_evidence"],
    },
    {
        "name": "troubleshooting-agent",
        "description": "Builds ordered diagnostic steps from manual citations, telemetry, and safety rules.",
        "inputs": ["message", "manual_evidence", "telemetry_snapshot"],
        "outputs": ["diagnostic_steps"],
    },
    {
        "name": "business-agent",
        "description": "Retrieves warranty, SLA, order, and service entitlement context.",
        "inputs": ["machine_id", "service_contract"],
        "outputs": ["business_evidence"],
    },
]


def list_agent_capabilities() -> list[dict]:
    return AGENT_CATALOG
