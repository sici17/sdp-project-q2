"""Domain models against the payloads the fleet dataset actually produces.

These exist because of a specific failure. The connectors were moved onto the
supplied dataset, but three domain models were left describing the shape of the
fixtures they replaced. ``from_dict`` read required keys that the real payloads
do not carry, so an ordinary question — "how do I order spare parts?" — raised a
``KeyError`` inside the repository and took the whole chat turn down.

Nothing about that was visible from the outside: the tests passed, because they
fed the models the old fixture shape. So these tests use payloads copied from
what the Business MCP query layer returns for the committed dataset.
"""

import pytest

from arol_ai.domain.models import OrderRecord, QuoteRecord, ServiceContract, ServiceHistoryRecord

# Copied from business.list_orders("MCH-0001") against the committed dataset.
ORDER_PAYLOAD = {
    "orderId": "ORD-2025-0001",
    "quoteId": "QTE-2025-0001",
    "companyId": "CMP-001",
    "orderStatus": "Closed",
    "orderDate": "2025-04-08",
    "expectedDeliveryDate": "2025-06-24",
    "shipmentStatus": "Installed",
    "currency": "EUR",
    "notes": "Aftermarket order converted from QTE-2025-0001, revision 2",
    "total": 45837.5,
    "lines": [
        {
            "quoteLineId": "QLN-0004",
            "machineId": "MCH-0001",
            "price": 36480.0,
            "description": "PK 314 closure head overhaul kit, 20 heads",
        }
    ],
}

# Copied from business.get_service_entitlement("MCH-0004").
ENTITLEMENT_PAYLOAD = {
    "machineId": "MCH-0004",
    "companyId": "CMP-003",
    "serialNumber": "17478",
    "modelCode": "M-EURO-VP-IES",
    "deliveryDate": "2019-07-22",
    "plantLocation": "Zaragoza Plant 1 - Filling Suite B",
    "acquisitionCost": 52600.0,
    "currency": "EUR",
    "openTicketCount": 1,
    "openTickets": [],
    "lastScheduledMaintenance": "2026-07-23",
    "ticketCount": 5,
    "warrantyRecord": None,
    "warrantyNote": "The fleet dataset contains no warranty or SLA records.",
}

# Copied from business.list_maintenance_tickets(machine_id="MCH-0004").
TICKET_PAYLOAD = {
    "ticketId": "TCK-0032",
    "machineId": "MCH-0004",
    "ticketType": "Scheduled maintenance",
    "ticketStatus": "In progress",
    "priority": "High",
    "createdDate": "2026-07-23",
    "ownerRole": "AROL Technical Service",
    "alarmId": None,
    "alarmCode": None,
    "alarmSeverity": None,
    "alarmTimestamp": None,
}

# Copied from business.list_quotes("CMP-002").
QUOTE_PAYLOAD = {
    "quoteId": "QTE-2026-0011",
    "companyId": "CMP-002",
    "currency": "EUR",
    "createdAt": "2026-03-30",
    "validUntil": "2026-05-14",
    "description": "Guard interlock safety retrofit on both EAGLE VA turrets",
    "currentRevisionId": "QREV-0022",
    "currentRevisionNumber": 3,
    "currentRevisionStatus": "Approved",
    "currentRevisionIssuedAt": "2026-05-06",
    "currentDiscountRate": 0.08,
    "currentChangeSummary": "Final scope confirmed for both turrets, 8% discount agreed",
    "revisionCount": 3,
    "currentRevisionTotal": 30452.0,
    "expired": 1,
}


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (OrderRecord, ORDER_PAYLOAD),
        (ServiceContract, ENTITLEMENT_PAYLOAD),
        (ServiceHistoryRecord, TICKET_PAYLOAD),
        (QuoteRecord, QUOTE_PAYLOAD),
    ],
)
def test_every_model_reads_the_payload_the_dataset_produces(model, payload) -> None:
    """The regression itself: no required key the real payload does not carry."""
    assert model.from_dict(payload) is not None


def test_an_order_keeps_the_lines_that_say_what_was_ordered() -> None:
    order = OrderRecord.from_dict(ORDER_PAYLOAD)

    assert order.order_id == "ORD-2025-0001"
    assert order.total_summary == "45,837.50 EUR"
    # Order lines carry fulfilment only, so the content comes from the quote
    # lines of the approved revision; losing them loses the answer.
    assert order.lines[0]["description"].startswith("PK 314")


def test_a_ticket_without_an_alarm_is_a_scheduled_job_not_missing_data() -> None:
    ticket = ServiceHistoryRecord.from_dict(TICKET_PAYLOAD)

    assert ticket.alarm_code is None
    assert ticket.ticket_type == "Scheduled maintenance"
    assert ticket.is_open is True


def test_service_standing_reports_what_it_has_and_names_what_it_lacks() -> None:
    contract = ServiceContract.from_dict(ENTITLEMENT_PAYLOAD)

    assert contract.delivery_date == "2019-07-22"
    assert contract.acquisition_summary == "52,600.00 EUR"
    assert "no warranty" in contract.coverage_note


def test_the_current_revision_carries_the_change_that_was_asked_about() -> None:
    """ "How did the latest revision change?" is one of the brief's questions."""
    quote = QuoteRecord.from_dict(QUOTE_PAYLOAD)

    assert quote.current_revision_number == 3
    assert quote.current_revision_status == "Approved"
    assert quote.change_summary == "Final scope confirmed for both turrets, 8% discount agreed"
    # Line prices are already net of the revision discount; the total must not
    # be discounted a second time.
    assert quote.current_total == 30452.0
    assert quote.expired is True


def test_a_rejected_current_revision_closes_the_quote() -> None:
    rejected = QuoteRecord.from_dict({**QUOTE_PAYLOAD, "currentRevisionStatus": "Rejected"})

    assert rejected.is_actionable is False
