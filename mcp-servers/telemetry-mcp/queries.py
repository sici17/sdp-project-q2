"""Telemetry and alarm queries, served from the converted fleet dataset."""

from __future__ import annotations

import dataset as _dataset


def source_name() -> str:
    return "dataset"


def machine_ids() -> list[str]:
    return list(_dataset.machine_ids())


def latest(machine_id: str) -> dict | None:
    return _dataset.latest(machine_id)


def history(machine_id: str, limit: int = 60) -> list[dict]:
    return _dataset.history(machine_id, limit)


def alarms(machine_id: str, limit: int = 50) -> list[dict]:
    return _dataset.alarms(machine_id, limit)


def alarm_frequency(machine_id: str, limit: int = 10) -> list[dict]:
    return _dataset.alarm_frequency(machine_id, limit)


def alarm_lookup(machine_id: str, code: str) -> dict | None:
    return _dataset.alarm_lookup(machine_id, code)


def get_latest_telemetry(machine_id: str) -> dict | None:
    """Retained for callers that imported the previous helper name."""
    return latest(machine_id)
