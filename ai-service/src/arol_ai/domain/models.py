from dataclasses import dataclass, field, replace
from datetime import datetime, timezone


@dataclass(frozen=True)
class Machine:
    id: str
    serial_number: str
    model: str
    plant: str
    status: str
    #: Owning company. The tenant boundary of the access model.
    company_id: str | None = None
    last_telemetry_at: str | None = None

    @classmethod
    def from_dict(cls, payload: dict) -> "Machine":
        return cls(
            id=payload["id"],
            serial_number=payload["serialNumber"],
            model=payload["model"],
            plant=payload["plant"],
            status=payload["status"],
            company_id=payload.get("companyId"),
            last_telemetry_at=payload.get("lastTelemetryAt"),
        )

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "serialNumber": self.serial_number,
            "model": self.model,
            "plant": self.plant,
            "status": self.status,
        }
        if self.company_id:
            payload["companyId"] = self.company_id
        if self.last_telemetry_at:
            payload["lastTelemetryAt"] = self.last_telemetry_at
        return payload


@dataclass(frozen=True)
class ManualReference:
    machine_id: str
    title: str
    version: str
    language: str
    url: str

    @classmethod
    def from_dict(cls, payload: dict) -> "ManualReference":
        return cls(
            machine_id=payload["machineId"],
            title=payload["title"],
            version=payload["version"],
            language=payload["language"],
            url=payload["url"],
        )

    def to_dict(self) -> dict:
        return {
            "machineId": self.machine_id,
            "title": self.title,
            "version": self.version,
            "language": self.language,
            "url": self.url,
        }


@dataclass(frozen=True)
class Manual:
    machine_id: str
    title: str
    version: str
    language: str
    url: str
    related_manuals: list[ManualReference] = field(default_factory=list)
    #: Leading pages before this manual's own page 1. A citation names the PDF
    #: index; the number printed on the paper is that index minus this.
    printed_page_offset: int = 0

    @classmethod
    def from_dict(cls, payload: dict) -> "Manual":
        return cls(
            machine_id=payload["machineId"],
            title=payload["title"],
            version=payload["version"],
            language=payload["language"],
            url=payload["url"],
            related_manuals=[
                ManualReference.from_dict(item) for item in payload.get("relatedManuals", [])
            ],
            printed_page_offset=int(payload.get("printedPageOffset") or 0),
        )

    def to_dict(self) -> dict:
        return {
            "machineId": self.machine_id,
            "title": self.title,
            "version": self.version,
            "language": self.language,
            "url": self.url,
            "printedPageOffset": self.printed_page_offset,
            "relatedManuals": [manual.to_dict() for manual in self.related_manuals],
        }


@dataclass(frozen=True)
class TelemetrySnapshot:
    machine_id: str
    timestamp: str
    temperature_c: float
    active_alarm: str
    health: str
    # The dataset's own signals. `rpm` and `torque_nm` used to sit here from the
    # simulator era; the supplied fleet records neither, so they arrived as 0.0
    # and were reported to operators as though measured.
    operational_status: str | None = None
    production_rate_bph: float | None = None
    #: This machine's rated speed, read from Machines.configurationProfile.
    nominal_rate_bph: float | None = None
    #: Rate as a share of this machine's own nominal. requirements/README.md is
    #: explicit that two machines of a model are not comparable on rate alone.
    rate_utilization_pct: float | None = None
    uptime_percentage: float | None = None
    alarm_count: int | None = None
    energy_kwh: float | None = None
    health_note: str | None = None
    source: str | None = None
    quality: str = "fresh"
    age_seconds: int | None = None
    stale_after_seconds: int | None = None
    missing_fields: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict) -> "TelemetrySnapshot":
        return cls(
            machine_id=payload["machineId"],
            timestamp=payload["timestamp"],
            temperature_c=payload["temperatureC"],
            active_alarm=payload["activeAlarm"],
            health=payload["health"],
            operational_status=payload.get("operationalStatus"),
            production_rate_bph=payload.get("productionRateBph"),
            nominal_rate_bph=payload.get("nominalRateBph"),
            rate_utilization_pct=payload.get("rateUtilizationPct"),
            uptime_percentage=payload.get("uptimePercentage"),
            alarm_count=payload.get("alarmCount"),
            energy_kwh=payload.get("energyKwh"),
            health_note=payload.get("healthNote"),
            source=payload.get("source"),
            quality=payload.get("quality", "fresh"),
            age_seconds=payload.get("ageSeconds"),
            stale_after_seconds=payload.get("staleAfterSeconds"),
            missing_fields=tuple(payload.get("missingFields", ())),
        )

    @property
    def has_active_alarm(self) -> bool:
        return self.active_alarm.upper() not in {"", "NONE", "NO_ALARM"}

    def with_quality(
        self,
        *,
        stale_after_seconds: int,
        now: datetime | None = None,
    ) -> "TelemetrySnapshot":
        if self.missing_fields:
            return replace(
                self,
                quality="partial",
                stale_after_seconds=stale_after_seconds,
            )

        observed_at = _parse_timestamp(self.timestamp)
        if observed_at is None:
            return replace(
                self,
                quality="invalid-timestamp",
                stale_after_seconds=stale_after_seconds,
            )

        # Telemetry timestamps come from the supplied dataset window, so age is
        # measured against the platform's frozen reference date. Using the wall
        # clock would mark every reading stale.
        from arol_ai.config import platform_now

        reference_time = now or platform_now()
        age_seconds = max(0, int((reference_time - observed_at).total_seconds()))
        quality = (
            "fresh" if stale_after_seconds <= 0 or age_seconds <= stale_after_seconds else "stale"
        )
        return replace(
            self,
            quality=quality,
            age_seconds=age_seconds,
            stale_after_seconds=stale_after_seconds,
        )

    def to_dict(self) -> dict:
        payload = {
            "machineId": self.machine_id,
            "timestamp": self.timestamp,
            "operationalStatus": self.operational_status,
            "productionRateBph": self.production_rate_bph,
            "nominalRateBph": self.nominal_rate_bph,
            "rateUtilizationPct": self.rate_utilization_pct,
            "uptimePercentage": self.uptime_percentage,
            "alarmCount": self.alarm_count,
            "temperatureC": self.temperature_c,
            "energyKwh": self.energy_kwh,
            "healthNote": self.health_note,
            "activeAlarm": self.active_alarm,
            "health": self.health,
        }
        if self.source:
            payload["source"] = self.source
        if self.quality:
            payload["quality"] = self.quality
        if self.age_seconds is not None:
            payload["ageSeconds"] = self.age_seconds
        if self.stale_after_seconds is not None:
            payload["staleAfterSeconds"] = self.stale_after_seconds
        if self.missing_fields:
            payload["missingFields"] = list(self.missing_fields)
        return payload


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def describe_reading(telemetry: TelemetrySnapshot) -> str:
    """The machine's current reading, in the dataset's own terms.

    Rate is quoted against this machine's nominal rather than on its own:
    requirements/README.md is explicit that two machines of one model can have
    very different rated speeds, so a bare bottles-per-hour figure says nothing
    about whether the reading is normal.
    """
    parts: list[str] = []
    if telemetry.production_rate_bph is not None:
        rate = f"{telemetry.production_rate_bph:,.0f} bph"
        if telemetry.rate_utilization_pct is not None:
            rate += f" ({telemetry.rate_utilization_pct:.0f}% of nominal)"
        parts.append(rate)
    if telemetry.uptime_percentage is not None:
        parts.append(f"{telemetry.uptime_percentage:.0f}% uptime")
    if telemetry.alarm_count is not None:
        alarm_word = "event" if telemetry.alarm_count == 1 else "events"
        parts.append(f"{telemetry.alarm_count} new alarm {alarm_word} in the last hour")
    parts.append(f"{telemetry.temperature_c} C")
    return ", ".join(parts)


@dataclass(frozen=True)
class ServiceContract:
    """A machine's service standing, as the fleet dataset is able to describe it.

    This used to model a warranty and an SLA. The supplied dataset carries
    neither: there is no warranty sheet, no coverage level and no contracted
    response time anywhere in the workbook. What it does carry is when the
    machine was delivered, what was paid for it, and its maintenance record.

    Rendering the absent fields as "Unavailable" would have been worse than
    leaving them out, because on a service screen that reads as *not covered*
    rather than *not recorded here*. So the coverage question is answered with a
    sentence that says where the answer actually lives.
    """

    machine_id: str
    company_id: str | None = None
    serial_number: str | None = None
    model_code: str | None = None
    delivery_date: str | None = None
    plant_location: str | None = None
    #: Total of the ordered quote lines for this machine, in ``currency``.
    acquisition_cost: float | None = None
    currency: str | None = None
    open_ticket_count: int | None = 0
    ticket_count: int | None = 0
    last_scheduled_maintenance: str | None = None
    #: Why there is no warranty status here, in words a user can read.
    coverage_note: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> "ServiceContract":
        return cls(
            machine_id=payload["machineId"],
            company_id=payload.get("companyId"),
            serial_number=payload.get("serialNumber"),
            model_code=payload.get("modelCode"),
            delivery_date=payload.get("deliveryDate"),
            plant_location=payload.get("plantLocation"),
            acquisition_cost=payload.get("acquisitionCost"),
            currency=payload.get("currency"),
            open_ticket_count=int(payload.get("openTicketCount") or 0),
            ticket_count=int(payload.get("ticketCount") or 0),
            last_scheduled_maintenance=payload.get("lastScheduledMaintenance"),
            coverage_note=str(payload.get("warrantyNote") or payload.get("coverageNote") or ""),
        )

    def to_dict(self) -> dict:
        return {
            "machineId": self.machine_id,
            "companyId": self.company_id,
            "serialNumber": self.serial_number,
            "modelCode": self.model_code,
            "deliveryDate": self.delivery_date,
            "plantLocation": self.plant_location,
            "acquisitionCost": self.acquisition_cost,
            "currency": self.currency,
            "openTicketCount": self.open_ticket_count,
            "ticketCount": self.ticket_count,
            "lastScheduledMaintenance": self.last_scheduled_maintenance,
            "coverageNote": self.coverage_note,
        }

    @property
    def acquisition_summary(self) -> str | None:
        if self.acquisition_cost is None or not self.currency:
            return None
        return f"{self.acquisition_cost:,.2f} {self.currency}"


@dataclass(frozen=True)
class OrderRecord:
    """An order as the dataset models it.

    An order has no items of its own: ``OrderLines`` tracks fulfilment, and what
    was actually ordered comes from the quote lines of the approved revision of
    its quote. Those lines arrive here as ``lines`` so the answer can say what an
    order contained without re-deriving the join.
    """

    order_id: str
    quote_id: str | None = None
    company_id: str | None = None
    status: str | None = None
    order_date: str | None = None
    expected_delivery_date: str | None = None
    shipment_status: str | None = None
    currency: str | None = None
    total: float | None = None
    notes: str | None = None
    lines: tuple[dict, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict) -> "OrderRecord":
        return cls(
            order_id=payload.get("orderId") or payload.get("orderNumber") or "",
            quote_id=payload.get("quoteId"),
            company_id=payload.get("companyId"),
            status=payload.get("orderStatus") or payload.get("status"),
            order_date=payload.get("orderDate") or payload.get("openedAt"),
            expected_delivery_date=(
                payload.get("expectedDeliveryDate") or payload.get("expectedAt")
            ),
            shipment_status=payload.get("shipmentStatus"),
            currency=payload.get("currency"),
            total=payload.get("total"),
            notes=payload.get("notes") or payload.get("summary"),
            lines=tuple(item for item in payload.get("lines") or () if isinstance(item, dict)),
        )

    def to_dict(self) -> dict:
        return {
            "orderId": self.order_id,
            "quoteId": self.quote_id,
            "companyId": self.company_id,
            "orderStatus": self.status,
            "orderDate": self.order_date,
            "expectedDeliveryDate": self.expected_delivery_date,
            "shipmentStatus": self.shipment_status,
            "currency": self.currency,
            "total": self.total,
            "notes": self.notes,
            "lines": list(self.lines),
        }

    @property
    def total_summary(self) -> str | None:
        if self.total is None or not self.currency:
            return None
        return f"{self.total:,.2f} {self.currency}"


@dataclass(frozen=True)
class ServiceHistoryRecord:
    """A maintenance ticket, which is what service history is in this dataset.

    Tickets that did not originate from an alarm carry no ``alarmId``; that is a
    scheduled job, not missing data, so the alarm fields are optional here.
    """

    ticket_id: str
    machine_id: str
    ticket_type: str | None = None
    status: str | None = None
    priority: str | None = None
    created_date: str | None = None
    owner_role: str | None = None
    alarm_code: str | None = None
    alarm_severity: str | None = None
    alarm_timestamp: str | None = None

    @classmethod
    def from_dict(cls, payload: dict) -> "ServiceHistoryRecord":
        return cls(
            ticket_id=payload.get("ticketId") or payload.get("id") or "",
            machine_id=payload.get("machineId", ""),
            ticket_type=payload.get("ticketType") or payload.get("type"),
            status=payload.get("ticketStatus") or payload.get("status"),
            priority=payload.get("priority"),
            created_date=payload.get("createdDate") or payload.get("serviceDate"),
            owner_role=payload.get("ownerRole") or payload.get("technician"),
            alarm_code=payload.get("alarmCode"),
            alarm_severity=payload.get("alarmSeverity"),
            alarm_timestamp=payload.get("alarmTimestamp"),
        )

    def to_dict(self) -> dict:
        return {
            "ticketId": self.ticket_id,
            "machineId": self.machine_id,
            "ticketType": self.ticket_type,
            "ticketStatus": self.status,
            "priority": self.priority,
            "createdDate": self.created_date,
            "ownerRole": self.owner_role,
            "alarmCode": self.alarm_code,
            "alarmSeverity": self.alarm_severity,
            "alarmTimestamp": self.alarm_timestamp,
        }

    @property
    def is_open(self) -> bool:
        return self.status in {"Open", "In progress", "Waiting for parts"}


@dataclass(frozen=True)
class QuoteRecord:
    """A quotation as the dataset models it.

    A quote has no status of its own: the lifecycle lives on its revisions, and
    the highest ``revisionNumber`` is the current one. Totals are the sum of that
    revision's line prices, which are already net of its discount and must not be
    discounted again.
    """

    quote_id: str
    company_id: str | None = None
    description: str | None = None
    currency: str | None = None
    created_at: str | None = None
    valid_until: str | None = None
    current_revision_number: int | None = None
    current_revision_status: str | None = None
    current_total: float | None = None
    revision_count: int = 0
    discount_rate: float | None = None
    change_summary: str | None = None
    expired: bool = False

    @classmethod
    def from_dict(cls, payload: dict) -> "QuoteRecord":
        return cls(
            quote_id=payload.get("quoteId", ""),
            company_id=payload.get("companyId"),
            description=payload.get("description"),
            currency=payload.get("currency"),
            created_at=payload.get("createdAt"),
            valid_until=payload.get("validUntil"),
            current_revision_number=payload.get("currentRevisionNumber"),
            current_revision_status=payload.get("currentRevisionStatus"),
            current_total=payload.get("currentRevisionTotal", payload.get("currentTotal")),
            revision_count=payload.get("revisionCount", 0) or 0,
            discount_rate=payload.get("currentDiscountRate"),
            change_summary=payload.get("currentChangeSummary", payload.get("changeSummary")),
            expired=bool(payload.get("expired")),
        )

    @property
    def is_actionable(self) -> bool:
        """False when the current revision closed the quote out."""
        return self.current_revision_status not in {"Rejected", "Expired", "Superseded"}

    def to_dict(self) -> dict:
        return {
            "quoteId": self.quote_id,
            "companyId": self.company_id,
            "description": self.description,
            "currency": self.currency,
            "validUntil": self.valid_until,
            "currentRevisionNumber": self.current_revision_number,
            "currentRevisionStatus": self.current_revision_status,
            "currentTotal": self.current_total,
            "revisionCount": self.revision_count,
            "expired": self.expired,
        }


@dataclass(frozen=True)
class MachineContext:
    machine: Machine | None
    manual: Manual | None
    telemetry: TelemetrySnapshot | None
    contract: ServiceContract | None
    orders: list[OrderRecord] = field(default_factory=list)
    service_history: list[ServiceHistoryRecord] = field(default_factory=list)
    #: Quotations issued to the machine's owning company. Quotes are commercial
    #: records of the company, not of one machine, so they are fetched by
    #: company and carried here alongside the machine they were asked about.
    quotes: list[QuoteRecord] = field(default_factory=list)
    business_error: str | None = None
    business_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "machine": self.machine.to_dict() if self.machine else None,
            "manual": self.manual.to_dict() if self.manual else None,
            "telemetry": self.telemetry.to_dict() if self.telemetry else None,
            "contract": self.contract.to_dict() if self.contract else None,
            "orders": [order.to_dict() for order in self.orders],
            "serviceHistory": [record.to_dict() for record in self.service_history],
        }
