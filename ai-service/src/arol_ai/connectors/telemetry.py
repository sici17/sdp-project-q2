from typing import Any, Protocol
from urllib.error import URLError

from arol_ai.config import Settings
from arol_ai.domain.models import TelemetrySnapshot
from arol_ai.mcp import HttpMcpTransport, McpClient, McpError


class TelemetryConnector(Protocol):
    name: str

    def latest_snapshot(self, machine_id: str) -> TelemetrySnapshot | None:
        """Return the latest telemetry snapshot for a machine, if available."""
        ...


class UnavailableTelemetryConnector:
    name = "telemetry.unavailable"

    def latest_snapshot(self, machine_id: str) -> TelemetrySnapshot | None:
        _ = machine_id
        return None


class McpTelemetryConnector:
    name = "telemetry.mcp"

    def __init__(
        self,
        base_url: str = "",
        *,
        client: McpClient | None = None,
        shared_secret: str | None = None,
        timeout_seconds: int = 3,
        stale_after_seconds: int = 900,
    ) -> None:
        # An injected client lets a caller keep this connector's snapshot
        # shaping while swapping the transport, which is how the offline
        # benchmark reaches the dataset without a telemetry service running.
        self.client = client or McpClient(
            HttpMcpTransport(
                base_url,
                shared_secret=shared_secret,
                timeout_seconds=timeout_seconds,
            ),
            client_name="arol-ai-telemetry-agent",
        )
        self.stale_after_seconds = stale_after_seconds

    def latest_snapshot(self, machine_id: str) -> TelemetrySnapshot | None:
        try:
            payload = self.client.call_tool(
                "telemetry.latest_snapshot",
                {"machineId": machine_id},
            )
        except (McpError, TimeoutError, URLError):
            return None
        return (
            _snapshot_from_payload(
                machine_id=machine_id,
                payload=payload,
                source=self.name,
                stale_after_seconds=self.stale_after_seconds,
            )
            if payload
            else None
        )


def build_telemetry_connector(settings: Settings) -> TelemetryConnector:
    if settings.telemetry_service_url:
        return McpTelemetryConnector(
            settings.telemetry_service_url,
            shared_secret=settings.mcp_shared_secret,
            stale_after_seconds=settings.telemetry_stale_seconds,
        )

    return UnavailableTelemetryConnector()


def _snapshot_from_payload(
    *,
    machine_id: str,
    payload: Any,
    source: str,
    stale_after_seconds: int,
) -> TelemetrySnapshot:
    if not isinstance(payload, dict):
        return TelemetrySnapshot(
            machine_id=machine_id,
            timestamp="",
            temperature_c=0.0,
            active_alarm="UNKNOWN",
            health="offline",
            source=source,
            quality="invalid",
            stale_after_seconds=stale_after_seconds,
        )

    missing_fields = tuple(
        field for field in REQUIRED_TELEMETRY_FIELDS if payload.get(field) is None
    )
    normalized = {
        "machineId": payload.get("machineId") or machine_id,
        "timestamp": payload.get("timestamp") or "",
        "operationalStatus": payload.get("operationalStatus"),
        "productionRateBph": payload.get("productionRateBph"),
        "nominalRateBph": payload.get("nominalRateBph"),
        "rateUtilizationPct": payload.get("rateUtilizationPct"),
        "uptimePercentage": payload.get("uptimePercentage"),
        "alarmCount": payload.get("alarmCount"),
        "temperatureC": payload.get("temperatureC", 0.0),
        "energyKwh": payload.get("energyKwh"),
        "healthNote": payload.get("healthNote"),
        "activeAlarm": payload.get("activeAlarm") or "UNKNOWN",
        "health": payload.get("health") or "offline",
        "source": payload.get("source") or source,
        "missingFields": missing_fields,
    }

    try:
        snapshot = TelemetrySnapshot.from_dict(normalized)
    except (TypeError, ValueError):
        return TelemetrySnapshot(
            machine_id=machine_id,
            timestamp=str(normalized.get("timestamp") or ""),
            temperature_c=0.0,
            active_alarm="UNKNOWN",
            health="offline",
            source=source,
            quality="invalid",
            stale_after_seconds=stale_after_seconds,
            missing_fields=missing_fields,
        )

    return snapshot.with_quality(stale_after_seconds=stale_after_seconds)


REQUIRED_TELEMETRY_FIELDS = (
    "machineId",
    "timestamp",
    "temperatureC",
    "activeAlarm",
    "health",
)
