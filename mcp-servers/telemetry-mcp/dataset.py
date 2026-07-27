"""Telemetry and alarms read from the converted AROL Q2 fleet dataset.

``TelemetrySnapshots`` is the measurement stream, aggregated per hour, and
``Alarms`` is the output of the monitoring platform's evaluation layer. Both
are produced by the platform rather than read from a machine display, which is
why alarms exist for machines with no operator panel of their own.

The dataset is a fixed window (2026-07-06 to 2026-08-04), so "now" is the
frozen reference date from the dataset brief rather than the wall clock.
Freshness is still computed, because the platform must be able to say how old a
reading is; it is simply computed against that frozen reference.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "arol_q2.sqlite"
)

# Snapshots summarise a one-hour interval, so a 15-minute staleness threshold
# would mark healthy hourly data as stale. Two hours allows for one missed hour.
DEFAULT_STALE_SECONDS = 7200

SOURCE_NAME = "fleet-dataset"

# Alarm severities from the dataset mapped onto the health vocabulary the rest
# of the platform already uses.
_CRITICAL_SEVERITIES = {"Critical", "High"}
_OPEN_STATUSES = {"Open", "Acknowledged"}
_PRODUCING_STATUSES = {"Running"}


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


@lru_cache(maxsize=1)
def _platform_today() -> str:
    override = os.environ.get("PLATFORM_TODAY")
    if override:
        return override
    rows = _query("SELECT value FROM dataset_meta WHERE key = 'platformToday';")
    return rows[0]["value"] if rows else "2026-08-05"


def reference_now() -> dt.datetime:
    """The frozen 'now' used for age, staleness and open-item reasoning."""
    return dt.datetime.fromisoformat(f"{_platform_today()}T00:00:00")


def _stale_after_seconds() -> int:
    try:
        return int(os.environ.get("TELEMETRY_STALE_SECONDS", DEFAULT_STALE_SECONDS))
    except ValueError:
        return DEFAULT_STALE_SECONDS


@lru_cache(maxsize=1)
def machine_ids() -> tuple[str, ...]:
    return tuple(row["machineId"] for row in _query("SELECT machineId FROM Machines ORDER BY machineId;"))


def _parse(timestamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None


def _health(row: dict, open_alarm: dict | None) -> str:
    if open_alarm and open_alarm.get("severity") in _CRITICAL_SEVERITIES:
        return "critical"
    if row.get("alarmCount") or row.get("operationalStatus") == "Alarm":
        return "warning"
    if row.get("operationalStatus") in _PRODUCING_STATUSES:
        return "ok"
    return "idle"


def _shape(row: dict, nominal_rate: int | None, open_alarm: dict | None) -> dict:
    """Return one snapshot in the platform's telemetry envelope."""
    timestamp = _parse(row["timestamp"])
    stale_after = _stale_after_seconds()
    age_seconds = (
        max(0, int((reference_now() - timestamp).total_seconds())) if timestamp else None
    )

    missing = [
        field
        for field in ("productionRateBph", "uptimePercentage", "temperatureC", "energyKwh")
        if row.get(field) is None
    ]

    if timestamp is None:
        quality = "invalid_timestamp"
    elif missing:
        quality = "partial"
    elif age_seconds is not None and age_seconds > stale_after:
        quality = "stale"
    else:
        quality = "fresh"

    rate = row.get("productionRateBph")
    return {
        "machineId": row["machineId"],
        "timestamp": f"{row['timestamp']}Z" if row.get("timestamp") else None,
        "operationalStatus": row.get("operationalStatus"),
        "productionRateBph": rate,
        "nominalRateBph": nominal_rate,
        # Share of this machine's own nominal rate, which is the only sound way
        # to judge whether a reading is normal: two machines of the same model
        # can have very different nominal rates.
        "rateUtilizationPct": (
            round(100 * rate / nominal_rate, 1)
            if rate is not None and nominal_rate else None
        ),
        "uptimePercentage": row.get("uptimePercentage"),
        "alarmCount": row.get("alarmCount"),
        "temperatureC": row.get("temperatureC"),
        "energyKwh": row.get("energyKwh"),
        "healthNote": row.get("healthNote"),
        "activeAlarm": open_alarm.get("alarmCode") if open_alarm else None,
        "health": _health(row, open_alarm),
        "source": SOURCE_NAME,
        "quality": quality,
        "ageSeconds": age_seconds,
        "staleAfterSeconds": stale_after,
        "missingFields": missing,
    }


@lru_cache(maxsize=32)
def _nominal_rate(machine_id: str) -> int | None:
    rows = _query("SELECT nominalRateBph FROM Machines WHERE machineId = ?;", (machine_id,))
    return rows[0]["nominalRateBph"] if rows else None


def _most_relevant_open_alarm(machine_id: str, before: str | None = None) -> dict | None:
    """The alarm that should be surfaced with a snapshot.

    Prefers unresolved alarms, most severe first, then most recent.
    """
    rows = _query(
        """
        SELECT alarmId, alarmCode, severity, alarmStatus, timestamp
        FROM Alarms
        WHERE machineId = ?
          AND (? IS NULL OR timestamp <= ?)
        ORDER BY
          CASE alarmStatus WHEN 'Open' THEN 0 WHEN 'Acknowledged' THEN 1 ELSE 2 END,
          CASE severity WHEN 'Critical' THEN 0 WHEN 'High' THEN 1
                        WHEN 'Medium' THEN 2 ELSE 3 END,
          timestamp DESC
        LIMIT 1;
        """,
        (machine_id, before, before),
    )
    if not rows:
        return None
    alarm = rows[0]
    return alarm if alarm["alarmStatus"] in _OPEN_STATUSES else None


def latest(machine_id: str) -> dict | None:
    """The most recent snapshot at or before the frozen reference date."""
    rows = _query(
        """
        SELECT * FROM TelemetrySnapshots
        WHERE machineId = ?
        ORDER BY timestamp DESC
        LIMIT 1;
        """,
        (machine_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return _shape(row, _nominal_rate(machine_id), _most_relevant_open_alarm(machine_id, row["timestamp"]))


def history(machine_id: str, limit: int = 60) -> list[dict]:
    """Recent snapshots, oldest first, bounded by ``limit``."""
    rows = _query(
        """
        SELECT * FROM TelemetrySnapshots
        WHERE machineId = ?
        ORDER BY timestamp DESC
        LIMIT ?;
        """,
        (machine_id, max(1, min(int(limit), 1440))),
    )
    nominal = _nominal_rate(machine_id)
    open_alarm = _most_relevant_open_alarm(machine_id)
    return [_shape(row, nominal, open_alarm if row.get("alarmCount") else None) for row in reversed(rows)]


def alarms(machine_id: str, limit: int = 50) -> list[dict]:
    """Recent alarms for a machine, unresolved first then most recent."""
    rows = _query(
        """
        SELECT a.alarmId, a.machineId, a.alarmCode, a.severity, a.alarmStatus,
               a.timestamp,
               t.ticketId, t.ticketStatus, t.ticketType
        FROM Alarms a
        LEFT JOIN MaintenanceTickets t ON t.alarmId = a.alarmId
        WHERE a.machineId = ?
        ORDER BY
          CASE a.alarmStatus WHEN 'Open' THEN 0 WHEN 'Acknowledged' THEN 1 ELSE 2 END,
          a.timestamp DESC
        LIMIT ?;
        """,
        (machine_id, max(1, min(int(limit), 500))),
    )

    return [
        {
            "machineId": row["machineId"],
            "alarmId": row["alarmId"],
            # `code` keeps the platform's existing field name; `alarmCode` is
            # the dataset's own, and answers cite it verbatim.
            "code": row["alarmCode"],
            "alarmCode": row["alarmCode"],
            "severity": row["severity"],
            "status": row["alarmStatus"],
            "startedAt": f"{row['timestamp']}Z",
            "clearedAt": None,
            "description": _describe(row["alarmCode"]),
            "linkedTicketId": row["ticketId"],
            "linkedTicketStatus": row["ticketStatus"],
            "source": SOURCE_NAME,
        }
        for row in rows
    ]


def alarm_frequency(machine_id: str, limit: int = 10) -> list[dict]:
    """Alarm codes ranked by how often this machine raised them.

    The entry point for "why is this machine generating repeated alarms?": the
    ranked codes are then looked up in that machine's own manual.
    """
    return _query(
        """
        SELECT alarmCode, severity, COUNT(*) AS occurrences,
               MAX(timestamp) AS lastSeen,
               SUM(CASE WHEN alarmStatus IN ('Open', 'Acknowledged') THEN 1 ELSE 0 END)
                   AS unresolved
        FROM Alarms
        WHERE machineId = ?
        GROUP BY alarmCode, severity
        ORDER BY occurrences DESC, lastSeen DESC
        LIMIT ?;
        """,
        (machine_id, max(1, min(int(limit), 50))),
    )


def alarm_lookup(machine_id: str, code: str) -> dict | None:
    """Resolve an ``ALnnn`` code from this machine's recorded alarm history.

    The short numeric code is matched only at the start of the dataset's full
    mnemonic. This prevents a semantic search miss from turning unrelated
    manual boilerplate into an apparent alarm definition.
    """
    match = re.fullmatch(
        r"AL[\s_-]?(\d{3})(?:_[A-Z0-9_]+)?",
        code.strip(),
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("code must be an AL code such as AL031.")

    short_code = f"AL{match.group(1)}"
    rows = _query(
        """
        SELECT alarmCode, severity, COUNT(*) AS occurrences,
               MIN(timestamp) AS firstSeen, MAX(timestamp) AS lastSeen,
               SUM(CASE WHEN alarmStatus IN ('Open', 'Acknowledged') THEN 1 ELSE 0 END)
                   AS unresolved
        FROM Alarms
        WHERE machineId = ?
          AND (UPPER(alarmCode) = ? OR UPPER(alarmCode) LIKE ? ESCAPE '\\')
        GROUP BY alarmCode, severity
        ORDER BY occurrences DESC, lastSeen DESC
        LIMIT 1;
        """,
        (machine_id, short_code, f"{short_code}\\_%"),
    )
    if not rows:
        return None
    row = rows[0]

    return {
        "machineId": machine_id,
        "requestedCode": short_code,
        "alarmCode": row["alarmCode"],
        "description": _describe(row["alarmCode"]),
        "severity": row["severity"],
        "occurrences": row["occurrences"],
        "unresolved": row["unresolved"],
        "firstSeen": f"{row['firstSeen']}Z",
        "lastSeen": f"{row['lastSeen']}Z",
        "source": SOURCE_NAME,
    }


def _describe(alarm_code: str | None) -> str:
    """Turn ``AL017_LOW_AIR_PRESSURE`` into a short human phrase.

    The mnemonic is the condition's short description; the machine's manual
    carries the cause and the remedy.
    """
    if not alarm_code or "_" not in alarm_code:
        return "Alarm condition reported by the monitoring platform."
    _, _, mnemonic = alarm_code.partition("_")
    phrase = mnemonic.replace("_", " ").lower()
    return f"Monitoring platform reported: {phrase}."
