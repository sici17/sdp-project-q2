from dataclasses import replace

from arol_ai.access import AccessContext
from arol_ai.config import get_settings
from arol_ai.connectors.business import BusinessConnectorError
from arol_ai.connectors.telemetry import _snapshot_from_payload
from arol_ai.data.manifest_repository import ManifestMachineRepository
from arol_ai.domain.models import ServiceContract, TelemetrySnapshot
from arol_ai.graph.orchestrator import Orchestrator


def test_manifest_repository_uses_injected_telemetry_and_business_connectors() -> None:
    settings = replace(
        get_settings(),
        telemetry_service_url=None,
    )
    repository = ManifestMachineRepository(
        settings,
        telemetry_connector=_TelemetryConnector(),
        business_connector=_BusinessConnector(),
    )

    context = repository.get_machine_context("MCH-0004")

    assert context.machine is not None
    # Identity comes from the manifest, which is the only machine source now.
    assert context.machine.serial_number == "17478"
    assert context.machine.status == "warning"
    assert context.manual is not None
    # Manuals in the fleet dataset are machine-specific, so a machine's manual
    # stands alone rather than pulling in a component manual.
    assert [manual.machine_id for manual in context.manual.related_manuals] == []
    assert context.telemetry is not None
    assert context.telemetry.active_alarm == "TORQUE_HIGH"
    assert context.contract is not None
    assert context.contract.delivery_date == "2019-04-18"


def test_document_only_request_does_not_depend_on_business_connector() -> None:
    settings = replace(
        get_settings(),
        telemetry_service_url=None,
    )
    business = _TrackingBusinessConnector()
    repository = ManifestMachineRepository(
        settings,
        telemetry_connector=_TelemetryConnector(),
        business_connector=business,
    )
    orchestrator = Orchestrator(repository=repository)

    context = orchestrator._request_context(
        {
            "machineId": "MCH-0004",
            "message": "Where is the torque adjustment procedure in the manual?",
        }
    )

    assert context.machine is not None
    assert business.calls == []


def test_warranty_request_isolated_from_unrelated_business_resources() -> None:
    settings = replace(
        get_settings(),
        telemetry_service_url=None,
    )
    business = _TrackingBusinessConnector(fail_orders=True, fail_service_history=True)
    repository = ManifestMachineRepository(
        settings,
        telemetry_connector=_TelemetryConnector(),
        business_connector=business,
    )
    orchestrator = Orchestrator(repository=repository)

    context = orchestrator._request_context(
        {
            "machineId": "MCH-0004",
            "message": "Is this machine still under warranty?",
            # Commercial data is only fetched for an identity allowed to read
            # it, so the request has to carry one.
            "access": AccessContext(user_id="USR-011", company_id="CMP-003", visibility="full"),
        }
    )

    assert context.contract is not None
    assert context.contract.delivery_date == "2019-04-18"
    assert context.business_errors == {}
    assert business.calls == ["entitlement"]


def test_business_partial_failure_preserves_successful_resources() -> None:
    settings = replace(
        get_settings(),
        telemetry_service_url=None,
    )
    repository = ManifestMachineRepository(
        settings,
        telemetry_connector=_TelemetryConnector(),
        business_connector=_TrackingBusinessConnector(fail_orders=True),
    )

    context = repository.get_machine_context("MCH-0004")

    assert context.contract is not None
    assert context.service_history == []
    assert context.business_errors == {"orders": "Order data is temporarily unavailable."}


def test_telemetry_payload_quality_marks_stale_snapshot() -> None:
    snapshot = _snapshot_from_payload(
        machine_id="euro-vp-2019-01",
        payload={
            "machineId": "MCH-0004",
            "timestamp": "2000-01-01T00:00:00Z",
            "productionRateBph": 9395,
            "nominalRateBph": 10000,
            "rateUtilizationPct": 94.0,
            "temperatureC": 41.1,
            "activeAlarm": "TORQUE_HIGH",
            "health": "warning",
        },
        source="test.telemetry",
        stale_after_seconds=900,
    )

    assert snapshot.quality == "stale"
    assert snapshot.age_seconds is not None
    assert snapshot.stale_after_seconds == 900


def test_telemetry_payload_quality_marks_partial_snapshot() -> None:
    snapshot = _snapshot_from_payload(
        machine_id="euro-vp-2019-01",
        payload={
            "machineId": "MCH-0004",
            "timestamp": "2026-05-23T10:15:00Z",
            "productionRateBph": 9395,
            "health": "warning",
        },
        source="test.telemetry",
        stale_after_seconds=900,
    )

    assert snapshot.quality == "partial"
    assert set(snapshot.missing_fields) == {
        "temperatureC",
        "activeAlarm",
    }


def test_telemetry_payload_quality_marks_invalid_payload_shape() -> None:
    snapshot = _snapshot_from_payload(
        machine_id="euro-vp-2019-01",
        payload=[],
        source="test.telemetry",
        stale_after_seconds=900,
    )

    assert snapshot.quality == "invalid"
    assert snapshot.health == "offline"
    assert snapshot.active_alarm == "UNKNOWN"


class _TelemetryConnector:
    name = "test.telemetry"

    def latest_snapshot(self, machine_id: str) -> TelemetrySnapshot | None:
        return TelemetrySnapshot(
            machine_id=machine_id,
            timestamp="2026-05-23T10:15:00.000Z",
            production_rate_bph=9395,
            nominal_rate_bph=10000,
            rate_utilization_pct=94.0,
            temperature_c=41.1,
            active_alarm="TORQUE_HIGH",
            health="warning",
            source=self.name,
        )


class _BusinessConnector:
    name = "test.business"

    def service_contract(self, machine_id: str) -> ServiceContract | None:
        return ServiceContract(
            machine_id=machine_id,
            company_id="CMP-001",
            delivery_date="2019-04-18",
            acquisition_cost=128400.0,
            currency="EUR",
            open_ticket_count=2,
            ticket_count=9,
            last_scheduled_maintenance="2026-07-11",
            coverage_note="The fleet dataset contains no warranty or SLA records.",
        )


class _TrackingBusinessConnector(_BusinessConnector):
    def __init__(
        self,
        *,
        fail_orders: bool = False,
        fail_service_history: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.fail_orders = fail_orders
        self.fail_service_history = fail_service_history

    def service_contract(self, machine_id: str) -> ServiceContract | None:
        self.calls.append("entitlement")
        return super().service_contract(machine_id)

    def orders(self, machine_id: str) -> list:
        self.calls.append("orders")
        if self.fail_orders:
            raise BusinessConnectorError("orders unavailable")
        return []

    def service_history(self, machine_id: str) -> list:
        self.calls.append("service_history")
        if self.fail_service_history:
            raise BusinessConnectorError("service history unavailable")
        return []
