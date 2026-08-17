"""Convert the supplied AROL Q2 fleet workbook into the platform's SQLite spine.

The workbook in ``requirements/`` is the authoritative source for companies,
users, machines, commercial history, telemetry, alarms and maintenance tickets.
Services must not parse spreadsheets at runtime, so this script performs a
one-shot conversion into ``data/arol_q2.sqlite``, which every service opens
read-only:

* gateway-service through ``node:sqlite``
* business-mcp and telemetry-mcp through the Python standard library
* ai-service only indirectly, through the MCP tool boundary

Usage
-----
    python scripts/convert_dataset.py              # rebuild the database
    python scripts/convert_dataset.py --check      # validate an existing one

Rebuilding needs ``openpyxl`` (see scripts/requirements-dev.txt). ``--check``
runs against the generated database alone and uses only the standard library,
so CI can validate the committed artifact without extra dependencies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKBOOK = REPO_ROOT / "requirements" / "AROL_Q2_synthetic_fleet_dataset.xlsx"
DEFAULT_DATABASE = REPO_ROOT / "data" / "arol_q2.sqlite"
DEFAULT_MANUALS_DIR = REPO_ROOT / "requirements" / "manuals"
DEFAULT_MANIFEST = REPO_ROOT / "data" / "manuals-manifest.json"

# The dataset brief fixes the reference "now" so that open items, overdue work
# and expiry dates stay stable. Telemetry ends 2026-08-04T23:00, so this date
# sees a complete final day.
PLATFORM_TODAY = "2026-08-05"

MANUAL_FILE_TEMPLATE = "{serial}_manual_EN.pdf"

# Column order per sheet. Declaring it here rather than trusting the header row
# keeps the SQLite schema stable even if the workbook is re-exported with
# columns reordered, and makes a renamed column a loud failure.
SHEETS: dict[str, tuple[str, ...]] = {
    "Companies": ("companyId", "companyName", "country", "sector", "city", "currency", "locale"),
    "Users": ("userId", "companyId", "firstName", "lastName", "email", "jobTitle", "visibility"),
    "MachineModels": (
        "modelId", "modelCode", "description", "primitiveDiameter", "nominalHeads",
        "containerType", "capType", "industrySegment", "notes",
    ),
    "Machines": (
        "machineId", "companyId", "modelId", "serialNumber", "deliveryDate",
        "plantLocation", "configurationProfile", "plcFamily", "softwareVersion",
    ),
    "Quotes": ("quoteId", "companyId", "currency", "createdAt", "validUntil", "description"),
    "QuoteRevisions": (
        "quoteRevisionId", "quoteId", "revisionNumber", "revisionStatus",
        "issuedAt", "discountRate", "changeSummary",
    ),
    "QuoteLines": ("quoteLineId", "quoteRevisionId", "machineId", "price", "description"),
    "Orders": (
        "orderId", "quoteId", "companyId", "orderStatus", "orderDate",
        "expectedDeliveryDate", "shipmentStatus", "currency", "notes",
    ),
    "OrderLines": ("orderLineId", "orderId", "fulfillmentStatus"),
    "TelemetrySnapshots": (
        "telemetryId", "machineId", "timestamp", "operationalStatus", "productionRateBph",
        "uptimePercentage", "alarmCount", "temperatureC", "energyKwh", "healthNote",
    ),
    "Alarms": ("alarmId", "machineId", "timestamp", "alarmCode", "severity", "alarmStatus"),
    "MaintenanceTickets": (
        "ticketId", "machineId", "alarmId", "ticketType", "ticketStatus",
        "priority", "createdDate", "ownerRole",
    ),
}

# Foreign keys the dataset brief documents as intentionally nullable. An inner
# join over these silently drops rows, so they are modelled as nullable and
# checked as such rather than treated as corruption.
NULLABLE_FOREIGN_KEYS = {
    ("QuoteLines", "machineId"),
    ("MaintenanceTickets", "alarmId"),
}

FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("Users", "companyId", "Companies", "companyId"),
    ("Machines", "companyId", "Companies", "companyId"),
    ("Machines", "modelId", "MachineModels", "modelId"),
    ("Quotes", "companyId", "Companies", "companyId"),
    ("QuoteRevisions", "quoteId", "Quotes", "quoteId"),
    ("QuoteLines", "quoteRevisionId", "QuoteRevisions", "quoteRevisionId"),
    ("QuoteLines", "machineId", "Machines", "machineId"),
    ("Orders", "quoteId", "Quotes", "quoteId"),
    ("Orders", "companyId", "Companies", "companyId"),
    ("OrderLines", "orderId", "Orders", "orderId"),
    ("TelemetrySnapshots", "machineId", "Machines", "machineId"),
    ("Alarms", "machineId", "Machines", "machineId"),
    ("MaintenanceTickets", "machineId", "Machines", "machineId"),
    ("MaintenanceTickets", "alarmId", "Alarms", "alarmId"),
)

# Controlled vocabularies from the dataset brief. A value outside these sets
# means either the workbook changed or an assumption in the code is wrong.
VOCABULARIES: dict[tuple[str, str], set[str]] = {
    ("Users", "visibility"): {"full", "technician", "commercial"},
    ("Machines", "plcFamily"): {
        "SIEMENS-SIMATIC-S7", "LINE-PLC-INTEGRATED", "HARDWIRED-CONTROL-PANEL",
    },
    ("QuoteRevisions", "revisionStatus"): {
        "Draft", "Submitted", "Superseded", "Approved", "Rejected", "Expired",
    },
    ("Orders", "orderStatus"): {"Confirmed", "In production", "Delivered", "Closed"},
    ("Orders", "shipmentStatus"): {
        "In production", "Ready for shipment", "Delivered", "Installed",
    },
    ("OrderLines", "fulfillmentStatus"): {"Manufacturing", "Ready for shipment", "Delivered"},
    ("TelemetrySnapshots", "operationalStatus"): {
        "Running", "Alarm", "Idle", "Stopped", "Maintenance", "Size change",
    },
    ("Alarms", "severity"): {"Critical", "High", "Medium", "Low"},
    ("Alarms", "alarmStatus"): {"Open", "Acknowledged", "Resolved"},
    ("MaintenanceTickets", "ticketType"): {
        "Remote troubleshooting", "On-site service", "Spare parts request",
        "Scheduled maintenance", "Overhaul", "Size change assistance",
    },
    ("MaintenanceTickets", "ticketStatus"): {
        "Open", "In progress", "Waiting for parts", "Resolved", "Closed",
    },
    ("MaintenanceTickets", "priority"): {"Critical", "High", "Medium", "Low"},
    ("MaintenanceTickets", "ownerRole"): {
        "Line Operator", "Maintenance Man", "Plant Maintenance Manager",
        "AROL Technical Service",
    },
}

INTEGER_COLUMNS = {
    ("MachineModels", "nominalHeads"),
    ("QuoteRevisions", "revisionNumber"),
    ("TelemetrySnapshots", "productionRateBph"),
    ("TelemetrySnapshots", "alarmCount"),
}

REAL_COLUMNS = {
    ("MachineModels", "primitiveDiameter"),
    ("QuoteRevisions", "discountRate"),
    ("QuoteLines", "price"),
    ("TelemetrySnapshots", "uptimePercentage"),
    ("TelemetrySnapshots", "temperatureC"),
    ("TelemetrySnapshots", "energyKwh"),
}


class DatasetError(RuntimeError):
    """Raised when the workbook or the generated database fails validation."""


# --------------------------------------------------------------------------
# Reading the workbook
# --------------------------------------------------------------------------

def _normalize_cell(value: object) -> object:
    """Return a SQLite-safe scalar, normalizing dates to ISO 8601 strings."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        # Midnight timestamps in date-only columns should not grow a time part.
        if value.time() == dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        return text if text else None
    return value


def read_workbook(workbook_path: Path) -> dict[str, list[dict[str, object]]]:
    try:
        import openpyxl
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise DatasetError(
            "openpyxl is required to rebuild the database.\n"
            "Install it with: python -m pip install -r scripts/requirements-dev.txt\n"
            "(--check mode needs no extra dependencies.)"
        ) from exc

    if not workbook_path.is_file():
        raise DatasetError(f"Workbook not found: {workbook_path}")

    book = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    tables: dict[str, list[dict[str, object]]] = {}

    for sheet_name, expected_columns in SHEETS.items():
        if sheet_name not in book.sheetnames:
            raise DatasetError(f"Workbook is missing the '{sheet_name}' sheet.")

        rows = list(book[sheet_name].iter_rows(values_only=True))
        if not rows:
            raise DatasetError(f"Sheet '{sheet_name}' is empty.")

        header = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
        missing = [column for column in expected_columns if column not in header]
        if missing:
            raise DatasetError(
                f"Sheet '{sheet_name}' is missing expected columns: {', '.join(missing)}"
            )

        index = {column: header.index(column) for column in expected_columns}
        records = []
        for raw in rows[1:]:
            if raw is None or all(cell is None for cell in raw):
                continue
            records.append(
                {
                    column: _normalize_cell(raw[position] if position < len(raw) else None)
                    for column, position in index.items()
                }
            )
        tables[sheet_name] = records

    book.close()
    return tables


# --------------------------------------------------------------------------
# Derived machine configuration
# --------------------------------------------------------------------------

_RATE_PATTERN = re.compile(r"(\d[\d.,]*)\s*bph", re.IGNORECASE)
_VOLTAGE_PATTERN = re.compile(r"(\d+)\s*V\s*-\s*(\d+)\s*Hz", re.IGNORECASE)
_HEADS_PATTERN = re.compile(r"(\d+)\s*heads?\b", re.IGNORECASE)
_SINGLE_HEAD_PATTERN = re.compile(r"\bsingle\s+head\b", re.IGNORECASE)


def parse_configuration_profile(profile: str | None) -> dict[str, object]:
    """Extract per-machine limits from ``Machines.configurationProfile``.

    Nominal production rate, supply voltage and head count have no dedicated
    columns; the brief states they are recorded here per machine and must be
    read before judging whether a telemetry reading is normal.
    """
    result: dict[str, object] = {
        "nominalRateBph": None,
        "supplyVoltage": None,
        "supplyFrequencyHz": None,
        "headsCount": None,
    }
    if not profile:
        return result

    rate = _RATE_PATTERN.search(profile)
    if rate:
        result["nominalRateBph"] = int(rate.group(1).replace(",", "").replace(".", ""))

    voltage = _VOLTAGE_PATTERN.search(profile)
    if voltage:
        result["supplyVoltage"] = int(voltage.group(1))
        result["supplyFrequencyHz"] = int(voltage.group(2))

    heads = _HEADS_PATTERN.search(profile)
    if heads:
        result["headsCount"] = int(heads.group(1))
    elif _SINGLE_HEAD_PATTERN.search(profile):
        result["headsCount"] = 1

    return result


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def _column_type(sheet: str, column: str) -> str:
    if (sheet, column) in INTEGER_COLUMNS:
        return "INTEGER"
    if (sheet, column) in REAL_COLUMNS:
        return "REAL"
    return "TEXT"


def _create_table_sql(sheet: str, columns: tuple[str, ...]) -> str:
    primary_key = columns[0]
    definitions = []
    for column in columns:
        declaration = f'  "{column}" {_column_type(sheet, column)}'
        if column == primary_key:
            declaration += " PRIMARY KEY"
        definitions.append(declaration)

    if sheet == "Machines":
        definitions.extend(
            [
                '  "manualFile" TEXT',
                '  "nominalRateBph" INTEGER',
                '  "supplyVoltage" INTEGER',
                '  "supplyFrequencyHz" INTEGER',
                '  "headsCount" INTEGER',
            ]
        )

    return f'CREATE TABLE "{sheet}" (\n' + ",\n".join(definitions) + "\n);"


VIEWS = (
    # The lifecycle state of a quote lives on its revisions, and the highest
    # revisionNumber is the current one. Every commercial answer starts here.
    """
    CREATE VIEW current_quote_revision AS
    SELECT r.*
    FROM QuoteRevisions r
    JOIN (
        SELECT quoteId, MAX(revisionNumber) AS maxRevision
        FROM QuoteRevisions
        GROUP BY quoteId
    ) latest ON latest.quoteId = r.quoteId AND latest.maxRevision = r.revisionNumber;
    """,
    # An order's content comes from the quote lines of its approved revision;
    # OrderLines only tracks fulfilment.
    """
    CREATE VIEW order_content AS
    SELECT o.orderId, o.companyId, o.orderStatus, o.shipmentStatus, o.currency,
           o.orderDate, o.expectedDeliveryDate,
           r.quoteRevisionId, r.revisionNumber, r.discountRate,
           l.quoteLineId, l.machineId, l.price, l.description
    FROM Orders o
    JOIN QuoteRevisions r ON r.quoteId = o.quoteId AND r.revisionStatus = 'Approved'
    JOIN QuoteLines l ON l.quoteRevisionId = r.quoteRevisionId;
    """,
    # Manuals are machine-specific and keyed by serial number.
    """
    CREATE VIEW machine_manual AS
    SELECT m.machineId, m.companyId, m.serialNumber, m.manualFile,
           m.plantLocation, m.configurationProfile, m.nominalRateBph,
           mm.modelId, mm.modelCode, mm.description AS modelDescription
    FROM Machines m
    JOIN MachineModels mm ON mm.modelId = m.modelId;
    """,
)

INDEXES = (
    "CREATE INDEX idx_users_company ON Users(companyId);",
    "CREATE INDEX idx_machines_company ON Machines(companyId);",
    "CREATE INDEX idx_machines_serial ON Machines(serialNumber);",
    "CREATE INDEX idx_quotes_company ON Quotes(companyId);",
    "CREATE INDEX idx_quote_revisions_quote ON QuoteRevisions(quoteId);",
    "CREATE INDEX idx_quote_lines_revision ON QuoteLines(quoteRevisionId);",
    "CREATE INDEX idx_quote_lines_machine ON QuoteLines(machineId);",
    "CREATE INDEX idx_orders_company ON Orders(companyId);",
    "CREATE INDEX idx_orders_quote ON Orders(quoteId);",
    "CREATE INDEX idx_order_lines_order ON OrderLines(orderId);",
    "CREATE INDEX idx_telemetry_machine_ts ON TelemetrySnapshots(machineId, timestamp);",
    "CREATE INDEX idx_alarms_machine_ts ON Alarms(machineId, timestamp);",
    "CREATE INDEX idx_alarms_code ON Alarms(alarmCode);",
    "CREATE INDEX idx_tickets_machine ON MaintenanceTickets(machineId);",
    "CREATE INDEX idx_tickets_alarm ON MaintenanceTickets(alarmId);",
)


def build_database(tables: dict[str, list[dict[str, object]]], database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE;")
        connection.execute("PRAGMA foreign_keys = ON;")

        for sheet, columns in SHEETS.items():
            connection.execute(_create_table_sql(sheet, columns))

        for sheet, columns in SHEETS.items():
            rows = tables[sheet]
            if sheet == "Machines":
                insert_columns = (
                    *columns, "manualFile", "nominalRateBph", "supplyVoltage",
                    "supplyFrequencyHz", "headsCount",
                )
                payload = []
                for row in rows:
                    derived = parse_configuration_profile(row.get("configurationProfile"))
                    serial = row.get("serialNumber")
                    payload.append(
                        tuple(row[column] for column in columns)
                        + (
                            MANUAL_FILE_TEMPLATE.format(serial=serial) if serial else None,
                            derived["nominalRateBph"],
                            derived["supplyVoltage"],
                            derived["supplyFrequencyHz"],
                            derived["headsCount"],
                        )
                    )
            else:
                insert_columns = columns
                payload = [tuple(row[column] for column in columns) for row in rows]

            placeholders = ", ".join("?" for _ in insert_columns)
            quoted = ", ".join(f'"{column}"' for column in insert_columns)
            connection.executemany(
                f'INSERT INTO "{sheet}" ({quoted}) VALUES ({placeholders});', payload
            )

        for statement in INDEXES:
            connection.execute(statement)
        for statement in VIEWS:
            connection.execute(statement)

        connection.execute(
            'CREATE TABLE dataset_meta ("key" TEXT PRIMARY KEY, "value" TEXT);'
        )
        meta = {
            "sourceWorkbook": DEFAULT_WORKBOOK.name,
            "platformToday": PLATFORM_TODAY,
            "manualFileTemplate": MANUAL_FILE_TEMPLATE,
            "generator": "scripts/convert_dataset.py",
            **{f"rowCount.{sheet}": str(len(rows)) for sheet, rows in tables.items()},
        }
        connection.executemany(
            "INSERT INTO dataset_meta (key, value) VALUES (?, ?);", sorted(meta.items())
        )
        connection.commit()
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _rows(connection: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return connection.execute(sql).fetchall()


def validate(database_path: Path, manuals_dir: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for the generated database.

    Errors mean the data contradicts the brief or the code's assumptions.
    Warnings flag the deliberately planted edge cases, which must be visible
    but must not fail the build: handling them correctly is part of the work.
    """
    if not database_path.is_file():
        raise DatasetError(
            f"Database not found: {database_path}\n"
            "Run 'python scripts/convert_dataset.py' first."
        )

    errors: list[str] = []
    warnings: list[str] = []
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)

    try:
        # Every declared table must exist and hold rows.
        for sheet in SHEETS:
            count = connection.execute(f'SELECT COUNT(*) FROM "{sheet}";').fetchone()[0]
            if count == 0:
                errors.append(f"{sheet}: table is empty.")

        # Referential integrity, honouring the documented nullable keys.
        for child, column, parent, parent_column in FOREIGN_KEYS:
            nullable = (child, column) in NULLABLE_FOREIGN_KEYS
            orphans = connection.execute(
                f'SELECT COUNT(*) FROM "{child}" c '
                f'LEFT JOIN "{parent}" p ON p."{parent_column}" = c."{column}" '
                f'WHERE c."{column}" IS NOT NULL AND p."{parent_column}" IS NULL;'
            ).fetchone()[0]
            if orphans:
                errors.append(
                    f"{child}.{column}: {orphans} row(s) reference a missing {parent}."
                )
            if nullable:
                empty = connection.execute(
                    f'SELECT COUNT(*) FROM "{child}" WHERE "{column}" IS NULL;'
                ).fetchone()[0]
                if empty:
                    warnings.append(
                        f"{child}.{column}: {empty} row(s) have no reference "
                        "(documented edge case - an inner join would drop them)."
                    )

        # Controlled vocabularies.
        for (sheet, column), allowed in VOCABULARIES.items():
            seen = {
                row[0]
                for row in connection.execute(
                    f'SELECT DISTINCT "{column}" FROM "{sheet}" WHERE "{column}" IS NOT NULL;'
                )
            }
            unexpected = seen - allowed
            if unexpected:
                errors.append(
                    f"{sheet}.{column}: values outside the controlled vocabulary: "
                    f"{', '.join(sorted(unexpected))}"
                )

        # Every machine must have its manual on disk, keyed by serial number.
        for row in _rows(connection, "SELECT machineId, serialNumber, manualFile FROM Machines;"):
            if not row["manualFile"]:
                errors.append(f"{row['machineId']}: no serial number, so no manual can be resolved.")
                continue
            if not (manuals_dir / row["manualFile"]).is_file():
                errors.append(
                    f"{row['machineId']} (serial {row['serialNumber']}): "
                    f"manual {row['manualFile']} not found in {manuals_dir}."
                )

        # Per-machine configuration must be parseable: telemetry judgements
        # depend on the machine's own nominal rate, not the model's.
        for row in _rows(
            connection, "SELECT machineId, configurationProfile, nominalRateBph FROM Machines;"
        ):
            if row["nominalRateBph"] is None:
                errors.append(
                    f"{row['machineId']}: no nominal rate parsed from configurationProfile "
                    f"({row['configurationProfile']!r})."
                )

        # Telemetry must never exceed the machine's own nominal rate.
        over_rate = _rows(
            connection,
            """
            SELECT t.machineId, COUNT(*) AS samples
            FROM TelemetrySnapshots t
            JOIN Machines m ON m.machineId = t.machineId
            WHERE m.nominalRateBph IS NOT NULL
              AND t.productionRateBph > m.nominalRateBph
            GROUP BY t.machineId;
            """,
        )
        for row in over_rate:
            errors.append(
                f"{row['machineId']}: {row['samples']} telemetry sample(s) exceed the "
                "machine's nominal production rate."
            )

        # uptimePercentage measures productive time, so it is 0 whenever the
        # machine is not producing.
        idle_uptime = connection.execute(
            """
            SELECT COUNT(*) FROM TelemetrySnapshots
            WHERE productionRateBph = 0 AND uptimePercentage > 0;
            """
        ).fetchone()[0]
        if idle_uptime:
            errors.append(
                f"TelemetrySnapshots: {idle_uptime} row(s) report uptime while not producing."
            )

        # alarmCount must agree with the Alarms sheet for the same machine-hour.
        snapshot_counts = {
            (row["machineId"], row["timestamp"][:13]): row["alarmCount"]
            for row in _rows(
                connection, "SELECT machineId, timestamp, alarmCount FROM TelemetrySnapshots;"
            )
        }
        alarm_counts: Counter[tuple[str, str]] = Counter()
        for row in _rows(connection, "SELECT machineId, timestamp FROM Alarms;"):
            alarm_counts[(row["machineId"], row["timestamp"][:13])] += 1

        mismatches = [
            key
            for key, expected in alarm_counts.items()
            if snapshot_counts.get(key, 0) != expected
        ]
        if mismatches:
            sample = ", ".join(f"{machine}@{hour}" for machine, hour in sorted(mismatches)[:3])
            errors.append(
                f"alarmCount disagrees with the Alarms sheet for {len(mismatches)} "
                f"machine-hour(s), e.g. {sample}."
            )

        # Quote revisions are numbered from 1 and must be contiguous.
        revisions: dict[str, list[int]] = defaultdict(list)
        for row in _rows(connection, "SELECT quoteId, revisionNumber FROM QuoteRevisions;"):
            revisions[row["quoteId"]].append(row["revisionNumber"])
        for quote_id, numbers in revisions.items():
            if sorted(numbers) != list(range(1, len(numbers) + 1)):
                errors.append(
                    f"{quote_id}: revision numbers are not contiguous from 1 ({sorted(numbers)})."
                )

        # Planted commercial edge cases: surface them so downstream code is
        # written to handle them rather than discovering them in a demo.
        for row in _rows(
            connection,
            """
            SELECT q.quoteId, r.revisionStatus
            FROM current_quote_revision r
            JOIN Quotes q ON q.quoteId = r.quoteId
            WHERE r.revisionStatus IN ('Rejected', 'Expired');
            """,
        ):
            warnings.append(
                f"{row['quoteId']}: current revision is {row['revisionStatus']} - "
                "answers must not present it as an active offer."
            )

        for row in _rows(
            connection,
            """
            SELECT o.orderId, o.orderDate, q.validUntil
            FROM Orders o
            JOIN Quotes q ON q.quoteId = o.quoteId
            WHERE date(o.orderDate) > date(q.validUntil);
            """,
        ):
            warnings.append(
                f"{row['orderId']}: ordered {row['orderDate']} after quote validity "
                f"expired {row['validUntil']} (documented inconsistency)."
            )

        companies_without_machines = _rows(
            connection,
            """
            SELECT c.companyId, c.companyName
            FROM Companies c
            LEFT JOIN Machines m ON m.companyId = c.companyId
            WHERE m.machineId IS NULL;
            """,
        )
        for row in companies_without_machines:
            warnings.append(
                f"{row['companyId']} ({row['companyName']}): has users but owns no machines - "
                "the fleet view must render an honest empty state."
            )

        # Every visibility level should be represented, otherwise the access
        # model cannot be demonstrated.
        seen_visibility = {
            row[0] for row in connection.execute("SELECT DISTINCT visibility FROM Users;")
        }
        for level in ("full", "technician", "commercial"):
            if level not in seen_visibility:
                errors.append(f"Users: no user has '{level}' visibility.")
    finally:
        connection.close()

    return errors, warnings


def detect_printed_page_offset(pdf_path: Path) -> int:
    """How many leading pages sit before the manual's own page 1.

    The app cites the PDF's physical page index. The manual prints its own
    number on every page, and front matter makes the two differ - by seven, for
    the whole of the reference corpus. An operator told "page 89" who opens a
    printed manual to page 89 lands seven pages away from the procedure, and
    several of those procedures are safety-critical.

    Each page that starts with a bare number votes for one offset. The offset is
    returned only when the corpus agrees overwhelmingly, because a manual whose
    numbering restarts partway through has no single answer and a wrong constant
    would be worse than none.
    """
    try:
        import fitz
    except ImportError:
        return 0

    votes: Counter[int] = Counter()
    try:
        with fitz.open(pdf_path) as document:
            for index, page in enumerate(document, start=1):
                text = page.get_text("text").strip()
                # The number sits in the header on some manuals and the
                # footer on others. Reading both ends took the reference
                # corpus from partial agreement to unanimous.
                printed = [
                    int(found.group(1))
                    for found in (
                        re.match(r"\s*(\d{1,3})(?!\d)", text),
                        re.search(r"(?<!\d)(\d{1,3})\s*\Z", text),
                    )
                    if found
                ]
                for number in printed:
                    offset = index - number
                    if 0 <= offset <= 40:
                        votes[offset] += 1
    except Exception:
        return 0

    if not votes:
        return 0

    offset, agreeing = votes.most_common(1)[0]
    total = sum(votes.values())
    # Two thirds is a deliberately blunt test: the reference manual agrees on
    # 121 of 121 pages, so anything near the boundary is a manual this cannot
    # describe with one number.
    return offset if total >= 20 and agreeing / total >= 0.66 else 0


def write_manual_manifest(database_path: Path, manifest_path: Path, manuals_dir: Path) -> int:
    """Generate the manual manifest from the fleet.

    Manuals are machine-specific, not model-specific: the file name carries the
    serial number, so ``Machines.serialNumber`` is the join key between the
    workbook and the documentation. Deriving the manifest here keeps that join
    from drifting as machines are added.
    """
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT machineId, companyId, serialNumber, manualFile, plantLocation,
                   modelCode, modelDescription, configurationProfile, nominalRateBph
            FROM machine_manual
            ORDER BY machineId;
            """
        ).fetchall()
    finally:
        connection.close()

    manuals = []
    for row in rows:
        if not row["manualFile"] or not (manuals_dir / row["manualFile"]).is_file():
            continue
        manuals.append(
            {
                "machineId": row["machineId"],
                "title": f"{row['modelCode']} - use and maintenance manual, serial {row['serialNumber']}",
                # The workbook carries no manual revision, so none is invented.
                "version": None,
                "language": "en",
                "fileName": row["manualFile"],
                "sourceUri": f"/manuals/{quote(row['manualFile'])}",
                # Printed page = cited PDF index - this. Zero when the manual
                # numbers its pages the same way the file does, or when it
                # cannot be established.
                "printedPageOffset": detect_printed_page_offset(
                    manuals_dir / row["manualFile"]
                ),
                "serialNumber": row["serialNumber"],
                "machine": {
                    "companyId": row["companyId"],
                    "model": row["modelCode"],
                    "modelDescription": row["modelDescription"],
                    "serialNumber": row["serialNumber"],
                    "plant": row["plantLocation"],
                    "configurationProfile": row["configurationProfile"],
                    "nominalRateBph": row["nominalRateBph"],
                },
            }
        )

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"manuals": manuals}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(manuals)


def summarize(database_path: Path) -> str:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        lines = []
        for sheet in SHEETS:
            count = connection.execute(f'SELECT COUNT(*) FROM "{sheet}";').fetchone()[0]
            lines.append(f"  {sheet:<20} {count:>6} rows")
        return "\n".join(lines)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--manuals", type=Path, default=DEFAULT_MANUALS_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate an existing database instead of rebuilding it.",
    )
    args = parser.parse_args(argv)

    try:
        if not args.check:
            print(f"Reading {args.workbook}")
            tables = read_workbook(args.workbook)
            build_database(tables, args.database)
            print(f"Wrote {args.database}")
            print(summarize(args.database))
            count = write_manual_manifest(args.database, args.manifest, args.manuals)
            print(f"Wrote {args.manifest} ({count} manuals)")

        errors, warnings = validate(args.database, args.manuals)
    except DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"note: {warning}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} validation error(s).", file=sys.stderr)
        return 1

    print(f"\nValidation passed ({len(warnings)} documented edge case(s) noted).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
