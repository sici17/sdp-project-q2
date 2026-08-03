"""Commercial queries over the converted AROL Q2 fleet dataset.

Replaces the previous hand-seeded JSON fixture. Every value returned here comes
from the supplied workbook, so the rules the dataset brief states about the
commercial model are enforced in one place:

* A quote has no status of its own; the lifecycle lives on its revisions, and
  the highest ``revisionNumber`` is the current one.
* ``QuoteLines.price`` is already net of the parent revision's ``discountRate``
  and must never be discounted a second time.
* ``OrderLines`` tracks fulfilment only. The content of an order comes from the
  quote lines of the approved revision of its quote.
* ``QuoteLines.machineId`` is empty on lines that do not refer to an installed
  machine, so joins to Machines must be left joins.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path

DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "arol_q2.sqlite"
)


def database_path() -> Path:
    return Path(os.environ.get("DATASET_DB_PATH", str(DEFAULT_DB_PATH)))


def _connect() -> sqlite3.Connection:
    path = database_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Fleet dataset not found at {path}. "
            "Run 'python scripts/convert_dataset.py' or set DATASET_DB_PATH."
        )
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _query(sql: str, params: tuple = ()) -> list[dict]:
    connection = _connect()
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def _query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


@lru_cache(maxsize=1)
def platform_today() -> str:
    """The dataset's frozen reference date, used for overdue and expiry logic."""
    override = os.environ.get("PLATFORM_TODAY")
    if override:
        return override
    row = _query_one("SELECT value FROM dataset_meta WHERE key = 'platformToday';")
    return row["value"] if row else "2026-08-05"


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

def machine_ids() -> list[str]:
    return [row["machineId"] for row in _query("SELECT machineId FROM Machines ORDER BY machineId;")]


def company_of_machine(machine_id: str) -> str | None:
    row = _query_one("SELECT companyId FROM Machines WHERE machineId = ?;", (machine_id,))
    return row["companyId"] if row else None


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------

def list_quotes(company_id: str) -> list[dict]:
    """Quotes for a company, each annotated with its current revision."""
    return _query(
        """
        SELECT q.quoteId, q.companyId, q.currency, q.createdAt, q.validUntil,
               q.description,
               r.quoteRevisionId AS currentRevisionId,
               r.revisionNumber  AS currentRevisionNumber,
               r.revisionStatus  AS currentRevisionStatus,
               r.issuedAt        AS currentRevisionIssuedAt,
               r.discountRate    AS currentDiscountRate,
               -- "How did the latest revision change?" is one of the questions
               -- the brief asks, so the summary travels with the quote rather
               -- than requiring a second call to compare revisions.
               r.changeSummary   AS currentChangeSummary,
               (SELECT COUNT(*) FROM QuoteRevisions x WHERE x.quoteId = q.quoteId)
                   AS revisionCount,
               (SELECT ROUND(SUM(l.price), 2) FROM QuoteLines l
                 WHERE l.quoteRevisionId = r.quoteRevisionId) AS currentRevisionTotal,
               CASE WHEN date(q.validUntil) < date(?) THEN 1 ELSE 0 END AS expired
        FROM Quotes q
        JOIN current_quote_revision r ON r.quoteId = q.quoteId
        WHERE q.companyId = ?
        ORDER BY q.createdAt DESC;
        """,
        (platform_today(), company_id),
    )


def get_quote(quote_id: str) -> dict | None:
    """A quote with its full revision history and the lines of each revision."""
    quote = _query_one("SELECT * FROM Quotes WHERE quoteId = ?;", (quote_id,))
    if not quote:
        return None

    revisions = _query(
        """
        SELECT quoteRevisionId, quoteId, revisionNumber, revisionStatus,
               issuedAt, discountRate, changeSummary
        FROM QuoteRevisions
        WHERE quoteId = ?
        ORDER BY revisionNumber;
        """,
        (quote_id,),
    )

    for revision in revisions:
        # Prices are already net of this revision's discountRate.
        revision["lines"] = _query(
            """
            SELECT quoteLineId, quoteRevisionId, machineId, price, description
            FROM QuoteLines
            WHERE quoteRevisionId = ?
            ORDER BY quoteLineId;
            """,
            (revision["quoteRevisionId"],),
        )
        revision["lineTotal"] = round(sum(line["price"] or 0 for line in revision["lines"]), 2)
        revision["isCurrent"] = revision["revisionNumber"] == max(
            item["revisionNumber"] for item in revisions
        )

    quote["revisions"] = revisions
    quote["revisionCount"] = len(revisions)
    quote["currentRevision"] = revisions[-1] if revisions else None
    quote["expired"] = bool(
        quote.get("validUntil") and quote["validUntil"] < platform_today()
    )
    return quote


def compare_quote_revisions(quote_id: str) -> dict | None:
    """Diff the two most recent revisions of a quote.

    Answers "how did the latest revision change?" without making the caller
    reason about line sets. Comparison is by line description, since line IDs
    are regenerated per revision.
    """
    quote = get_quote(quote_id)
    if not quote or len(quote["revisions"]) < 2:
        return {
            "quoteId": quote_id,
            "comparable": False,
            "reason": "Only one revision exists for this quote.",
        } if quote else None

    previous, current = quote["revisions"][-2], quote["revisions"][-1]
    previous_lines = {line["description"]: line for line in previous["lines"]}
    current_lines = {line["description"]: line for line in current["lines"]}

    added = [line for key, line in current_lines.items() if key not in previous_lines]
    removed = [line for key, line in previous_lines.items() if key not in current_lines]
    repriced = [
        {
            "description": key,
            "previousPrice": previous_lines[key]["price"],
            "currentPrice": line["price"],
        }
        for key, line in current_lines.items()
        if key in previous_lines and previous_lines[key]["price"] != line["price"]
    ]

    return {
        "quoteId": quote_id,
        "comparable": True,
        "previousRevision": {
            "revisionNumber": previous["revisionNumber"],
            "revisionStatus": previous["revisionStatus"],
            "discountRate": previous["discountRate"],
            "lineTotal": previous["lineTotal"],
        },
        "currentRevision": {
            "revisionNumber": current["revisionNumber"],
            "revisionStatus": current["revisionStatus"],
            "discountRate": current["discountRate"],
            "lineTotal": current["lineTotal"],
            "changeSummary": current["changeSummary"],
        },
        "addedLines": added,
        "removedLines": removed,
        "repricedLines": repriced,
        "totalDelta": round(current["lineTotal"] - previous["lineTotal"], 2),
    }


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

def list_orders(machine_id: str | None = None, company_id: str | None = None) -> list[dict]:
    """Orders for a machine or a company, with content from the approved revision."""
    if machine_id:
        orders = _query(
            """
            SELECT DISTINCT o.*
            FROM Orders o
            JOIN QuoteRevisions r ON r.quoteId = o.quoteId AND r.revisionStatus = 'Approved'
            JOIN QuoteLines l ON l.quoteRevisionId = r.quoteRevisionId
            WHERE l.machineId = ?
            ORDER BY o.orderDate DESC;
            """,
            (machine_id,),
        )
    elif company_id:
        orders = _query(
            "SELECT * FROM Orders WHERE companyId = ? ORDER BY orderDate DESC;",
            (company_id,),
        )
    else:
        return []

    for order in orders:
        order["lines"] = _query(
            """
            SELECT l.quoteLineId, l.machineId, l.price, l.description,
                   r.revisionNumber, r.discountRate
            FROM QuoteRevisions r
            JOIN QuoteLines l ON l.quoteRevisionId = r.quoteRevisionId
            WHERE r.quoteId = ? AND r.revisionStatus = 'Approved'
            ORDER BY l.quoteLineId;
            """,
            (order["quoteId"],),
        )
        order["fulfillment"] = _query(
            "SELECT orderLineId, fulfillmentStatus FROM OrderLines WHERE orderId = ? "
            "ORDER BY orderLineId;",
            (order["orderId"],),
        )
        order["total"] = round(sum(line["price"] or 0 for line in order["lines"]), 2)
    return orders


def machine_purchase_summary(machine_id: str) -> dict | None:
    """Delivery and acquisition cost for one machine.

    Cost is the sum of the approved-revision quote lines that name this machine,
    which is what "how much did it cost" means in this dataset.
    """
    machine = _query_one(
        """
        SELECT m.machineId, m.companyId, m.serialNumber, m.deliveryDate,
               m.plantLocation, m.configurationProfile, m.plcFamily,
               m.softwareVersion, m.nominalRateBph, m.headsCount,
               mm.modelCode, mm.description AS modelDescription
        FROM Machines m
        JOIN MachineModels mm ON mm.modelId = m.modelId
        WHERE m.machineId = ?;
        """,
        (machine_id,),
    )
    if not machine:
        return None

    lines = _query(
        """
        SELECT l.quoteLineId, l.price, l.description, o.orderId, o.orderDate,
               o.orderStatus, o.shipmentStatus, o.currency, r.revisionNumber
        FROM QuoteLines l
        JOIN QuoteRevisions r ON r.quoteRevisionId = l.quoteRevisionId
        JOIN Orders o ON o.quoteId = r.quoteId
        WHERE l.machineId = ? AND r.revisionStatus = 'Approved'
        ORDER BY o.orderDate;
        """,
        (machine_id,),
    )

    machine["orderedLines"] = lines
    machine["orderedTotal"] = round(sum(line["price"] or 0 for line in lines), 2) if lines else None
    machine["currency"] = lines[0]["currency"] if lines else None
    return machine


# --------------------------------------------------------------------------
# Maintenance tickets (service history)
# --------------------------------------------------------------------------

def list_maintenance_tickets(
    machine_id: str | None = None, company_id: str | None = None
) -> list[dict]:
    """Maintenance tickets, joined to their originating alarm where one exists.

    ``MaintenanceTickets.alarmId`` is empty for tickets that did not originate
    from an alarm, so this is a left join by design.
    """
    if machine_id:
        where, params = "t.machineId = ?", (machine_id,)
    elif company_id:
        where, params = "m.companyId = ?", (company_id,)
    else:
        return []

    return _query(
        f"""
        SELECT t.ticketId, t.machineId, t.alarmId, t.ticketType, t.ticketStatus,
               t.priority, t.createdDate, t.ownerRole,
               a.alarmCode, a.severity AS alarmSeverity, a.timestamp AS alarmTimestamp
        FROM MaintenanceTickets t
        JOIN Machines m ON m.machineId = t.machineId
        LEFT JOIN Alarms a ON a.alarmId = t.alarmId
        WHERE {where}
        ORDER BY t.createdDate DESC;
        """,
        params,
    )


# --------------------------------------------------------------------------
# Compatibility surface
# --------------------------------------------------------------------------

def get_service_entitlement(machine_id: str) -> dict | None:
    """Service standing for a machine, derived strictly from dataset facts.

    The workbook carries no warranty or SLA sheet, so nothing of that kind is
    invented here. What it does carry is delivery date, purchase history and
    the maintenance record, which is what a service conversation actually needs.
    """
    summary = machine_purchase_summary(machine_id)
    if not summary:
        return None

    tickets = list_maintenance_tickets(machine_id=machine_id)
    open_tickets = [
        ticket for ticket in tickets
        if ticket["ticketStatus"] in {"Open", "In progress", "Waiting for parts"}
    ]
    scheduled = [ticket for ticket in tickets if ticket["ticketType"] == "Scheduled maintenance"]

    return {
        "machineId": machine_id,
        "companyId": summary["companyId"],
        "serialNumber": summary["serialNumber"],
        "modelCode": summary["modelCode"],
        "deliveryDate": summary["deliveryDate"],
        "plantLocation": summary["plantLocation"],
        "acquisitionCost": summary["orderedTotal"],
        "currency": summary["currency"],
        "openTicketCount": len(open_tickets),
        "openTickets": open_tickets[:5],
        "lastScheduledMaintenance": scheduled[0]["createdDate"] if scheduled else None,
        "ticketCount": len(tickets),
        # Stated explicitly so no caller mistakes absence for "not covered".
        "warrantyRecord": None,
        "warrantyNote": (
            "The fleet dataset contains no warranty or SLA records. "
            "Coverage questions must be referred to AROL service."
        ),
    }


def list_service_history(machine_id: str) -> list[dict]:
    """Service history is the machine's maintenance ticket record."""
    return list_maintenance_tickets(machine_id=machine_id)
