from datetime import date

from arol_ai.config import platform_today
from arol_ai.domain.models import OrderRecord, ServiceContract, ServiceHistoryRecord
from arol_ai.graph.state import Evidence


def build_business_evidence(contract: ServiceContract) -> Evidence:
    parts = []
    if contract.delivery_date:
        parts.append(f"Delivered {contract.delivery_date}")
    if contract.acquisition_summary:
        parts.append(f"acquisition value {contract.acquisition_summary}")
    if contract.open_ticket_count is not None:
        parts.append(f"{contract.open_ticket_count} open maintenance ticket(s)")
    if contract.last_scheduled_maintenance:
        parts.append(
            f"last scheduled maintenance {contract.last_scheduled_maintenance}"
            f" ({_maintenance_age_note(contract.last_scheduled_maintenance)})"
        )
    if contract.coverage_note:
        parts.append(contract.coverage_note)

    return Evidence(
        source="business",
        title="Service standing",
        excerpt="; ".join(parts),
        confidence=0.86,
    )


def build_order_evidence(orders: list[OrderRecord]) -> Evidence:
    recent = sorted(orders, key=lambda item: item.order_date or "", reverse=True)
    summary = "; ".join(
        (
            f"{item.order_id} is {item.status or 'in an unknown state'}"
            + (f", ordered {item.order_date}" if item.order_date else "")
            + (f", expected {item.expected_delivery_date}" if item.expected_delivery_date else "")
            + (f", {item.total_summary}" if item.total_summary else "")
        )
        for item in recent[:5]
    )
    return Evidence(
        source="business",
        title="Order history",
        excerpt=summary,
        confidence=0.9,
    )


def build_service_history_evidence(records: list[ServiceHistoryRecord]) -> Evidence:
    recent = sorted(records, key=lambda item: item.created_date or "", reverse=True)
    summary = "; ".join(
        (
            f"{item.created_date}: {item.ticket_id} {item.ticket_type or 'ticket'} "
            f"is {item.status or 'in an unknown state'}"
            + (f", from alarm {item.alarm_code}" if item.alarm_code else "")
        )
        for item in recent[:5]
    )
    return Evidence(
        source="business",
        title="Maintenance history",
        excerpt=summary,
        confidence=0.9,
    )


def _maintenance_age_note(last_scheduled: str) -> str:
    """How long ago the last scheduled maintenance was.

    The dataset records no next-service date, so this reports elapsed time
    rather than inventing a due date. Measured against the frozen reference day,
    so the answer stays the same however long after delivery it is asked.
    """
    try:
        performed = date.fromisoformat(last_scheduled)
    except ValueError:
        return "date not recognised"

    days = (platform_today() - performed).days
    if days < 0:
        return "scheduled ahead"
    if days == 0:
        return "today"
    return f"{days} day{'s' if days != 1 else ''} ago"
