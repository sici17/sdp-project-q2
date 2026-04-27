from dataclasses import dataclass

from arol_ai.agents.telemetry_agent import build_telemetry_evidence
from arol_ai.alarm_codes import alarm_condition_label, extract_alarm_code
from arol_ai.domain.models import TelemetrySnapshot, describe_reading
from arol_ai.graph.state import Evidence
from arol_ai.mcp import McpClient, McpError
from arol_ai.tools.base import ToolCallRecord, ToolStatus


@dataclass(frozen=True)
class TelemetrySnapshotResult:
    evidence: Evidence | None
    tool_call: ToolCallRecord


class TelemetrySnapshotTool:
    name = "telemetry.latest_snapshot"
    agent = "telemetry-agent"

    def run(self, *, telemetry: TelemetrySnapshot | None) -> TelemetrySnapshotResult:
        if telemetry is None:
            return TelemetrySnapshotResult(
                evidence=None,
                tool_call=ToolCallRecord.create(
                    name=self.name,
                    agent=self.agent,
                    status="empty",
                    input_summary="latest telemetry requested",
                    output_summary="No telemetry snapshot was available.",
                ),
            )

        return TelemetrySnapshotResult(
            evidence=build_telemetry_evidence(telemetry),
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary=f"latest telemetry for {telemetry.machine_id}",
                output_summary=(
                    f"Health {telemetry.health}; alarm {telemetry.active_alarm}; "
                    f"{describe_reading(telemetry)}; "
                    f"quality {telemetry.quality}."
                ),
            ),
        )


@dataclass(frozen=True)
class AlarmCodeLookupResult:
    evidence: Evidence | None
    tool_call: ToolCallRecord


class AlarmCodeLookupTool:
    name = "telemetry.alarm_lookup"
    agent = "telemetry-agent"

    def __init__(self, *, mcp_client: McpClient | None = None) -> None:
        self.mcp_client = mcp_client

    def run(self, *, machine_id: str, query: str) -> AlarmCodeLookupResult:
        requested_code = extract_alarm_code(query)
        if requested_code is None:
            return self._empty(
                query=query,
                status="empty",
                summary="No ALnnn code was present in the operator's question.",
            )
        if self.mcp_client is None:
            return self._empty(
                query=query,
                status="error",
                summary="Telemetry MCP is not configured for exact alarm lookup.",
            )

        try:
            payload = self.mcp_client.call_tool(
                self.name,
                {"machineId": machine_id, "code": requested_code},
            )
        except (McpError, TimeoutError, OSError) as exc:
            return self._empty(
                query=query,
                status="error",
                summary=f"Exact alarm lookup failed: {exc}",
            )

        if not isinstance(payload, dict) or not payload.get("alarmCode"):
            return self._empty(
                query=query,
                status="empty",
                summary=f"No {requested_code} record was found for {machine_id}.",
            )

        full_code = str(payload["alarmCode"])
        severity = str(payload.get("severity") or "Unknown")
        occurrences = _safe_int(payload.get("occurrences"))
        unresolved = _safe_int(payload.get("unresolved"))
        last_seen = str(payload.get("lastSeen") or "unknown")
        condition = alarm_condition_label(full_code)
        occurrence_word = "occurrence" if occurrences == 1 else "occurrences"
        unresolved_word = "occurrence is" if unresolved == 1 else "occurrences are"
        excerpt = (
            f"{requested_code} means {condition}. The recorded code is {full_code} "
            f"with {severity.lower()} severity. This machine has {occurrences} {occurrence_word}; "
            f"{unresolved} {unresolved_word} unresolved. Last seen {last_seen}."
        )

        return AlarmCodeLookupResult(
            evidence=Evidence(
                source="telemetry",
                title=f"{requested_code} - {condition}",
                excerpt=excerpt,
                confidence=1.0,
                section="Alarm history",
                chunk_kind="telemetry-record",
                topics=("alarm", "telemetry"),
                alarm_codes=(full_code,),
            ),
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status="ok",
                input_summary=f"exact alarm lookup for {requested_code} on {machine_id}",
                output_summary=(
                    f"Resolved {requested_code} to {full_code}; {occurrences} occurrence(s), "
                    f"{unresolved} unresolved."
                ),
            ),
        )

    def _empty(
        self,
        *,
        query: str,
        status: ToolStatus,
        summary: str,
    ) -> AlarmCodeLookupResult:
        return AlarmCodeLookupResult(
            evidence=None,
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status=status,
                input_summary=f"exact alarm lookup for: {query[:160]}",
                output_summary=summary,
            ),
        )


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
