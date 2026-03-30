from arol_ai.config import Settings, get_settings
from arol_ai.connectors.business import (
    BusinessConnector,
    BusinessConnectorError,
    build_business_connector,
)
from arol_ai.connectors.telemetry import TelemetryConnector, build_telemetry_connector
from arol_ai.domain.models import (
    Machine,
    MachineContext,
    Manual,
    ManualReference,
    TelemetrySnapshot,
)
from arol_ai.rag.ingestion import _manuals_dir, load_manifest
from arol_ai.rag.models import ManualManifestEntry


class ManifestMachineRepository:
    """Machine context repository backed by real manual metadata and optional services."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        telemetry_connector: TelemetryConnector | None = None,
        business_connector: BusinessConnector | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.manuals_dir = _manuals_dir(self.settings)
        self.telemetry = telemetry_connector or build_telemetry_connector(self.settings)
        self.business = business_connector or build_business_connector(self.settings)

    def get_machine_context(
        self,
        machine_id: str,
        *,
        require_business: bool = False,
        include_business: bool = True,
        include_telemetry: bool = True,
        business_resources: set[str] | None = None,
    ) -> MachineContext:
        entry = self._find_entry(machine_id)

        if entry is None:
            return MachineContext(
                machine=None,
                manual=None,
                telemetry=None,
                contract=None,
            )

        telemetry = self.telemetry.latest_snapshot(machine_id) if include_telemetry else None
        contract = None
        orders = []
        service_history = []
        quotes = []
        business_error = None
        business_errors: dict[str, str] = {}
        if include_business:
            requested = business_resources or {
                "entitlement",
                "orders",
                "service_history",
                "quotes",
            }
            if "entitlement" in requested:
                try:
                    contract = self.business.service_contract(machine_id)
                except BusinessConnectorError:
                    business_errors["entitlement"] = (
                        "Service entitlement data is temporarily unavailable."
                    )
            if "orders" in requested:
                try:
                    orders = getattr(
                        self.business,
                        "orders",
                        lambda _machine_id: [],
                    )(machine_id)
                except BusinessConnectorError:
                    business_errors["orders"] = "Order data is temporarily unavailable."
            if "service_history" in requested:
                try:
                    service_history = getattr(
                        self.business,
                        "service_history",
                        lambda _machine_id: [],
                    )(machine_id)
                except BusinessConnectorError:
                    business_errors["service_history"] = (
                        "Service history is temporarily unavailable."
                    )
            if "quotes" in requested:
                company_id = getattr(entry, "company_id", None)
                if company_id:
                    try:
                        quotes = getattr(
                            self.business,
                            "quotes",
                            lambda _company_id: [],
                        )(company_id)
                    except BusinessConnectorError:
                        business_errors["quotes"] = "Quotation data is temporarily unavailable."
            if business_errors:
                if require_business:
                    raise BusinessConnectorError("A required business resource is unavailable.")
                business_error = "Some business data is temporarily unavailable."

        return MachineContext(
            machine=self._machine(
                machine_id=machine_id,
                entry=entry,
                telemetry=telemetry,
            ),
            manual=self._manual(entry),
            telemetry=telemetry,
            contract=contract,
            orders=orders,
            service_history=service_history,
            quotes=quotes,
            business_error=business_error,
            business_errors=business_errors,
        )

    def list_machine_ids(self) -> list[str]:
        return sorted(entry.machine_id for entry in load_manifest(self.manuals_dir))

    def _find_entry(self, machine_id: str) -> ManualManifestEntry | None:
        return next(
            (entry for entry in load_manifest(self.manuals_dir) if entry.machine_id == machine_id),
            None,
        )

    def _related_entries(self, machine_id: str) -> list[ManualManifestEntry]:
        return [
            entry
            for entry in load_manifest(self.manuals_dir)
            if machine_id in entry.related_machine_ids
        ]

    def _manual(self, entry: ManualManifestEntry | None) -> Manual | None:
        if entry is None:
            return None

        related_entries = self._related_entries(entry.machine_id)

        return Manual(
            machine_id=entry.machine_id,
            title=entry.title,
            version=entry.version,
            language=entry.language,
            url=entry.source_uri,
            printed_page_offset=entry.printed_page_offset,
            related_manuals=[
                ManualReference(
                    machine_id=related.machine_id,
                    title=related.title,
                    version=related.version,
                    language=related.language,
                    url=related.source_uri,
                )
                for related in _dedupe_entries(related_entries)
            ],
        )

    @staticmethod
    def _machine(
        *,
        machine_id: str,
        entry: ManualManifestEntry | None,
        telemetry: TelemetrySnapshot | None,
    ) -> Machine:
        return Machine(
            id=machine_id,
            company_id=entry.company_id if entry else None,
            serial_number=entry.serial_number if entry else "Unavailable",
            model=entry.machine_model if entry else "Unavailable",
            plant=entry.plant if entry else "Unavailable",
            # A machine with no reading is offline as far as the console is
            # concerned; the manifest carries no status of its own.
            status=telemetry.health if telemetry else "offline",
            last_telemetry_at=telemetry.timestamp if telemetry else None,
        )


def _dedupe_entries(entries: list[ManualManifestEntry]) -> list[ManualManifestEntry]:
    deduped: list[ManualManifestEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.machine_id in seen:
            continue
        seen.add(entry.machine_id)
        deduped.append(entry)

    return deduped
