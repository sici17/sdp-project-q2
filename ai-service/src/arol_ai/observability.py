"""Dependency-free request correlation and Prometheus metrics for the AI service."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from time import perf_counter
from typing import Iterator

HISTOGRAM_BUCKETS = (50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000)
_request_id: ContextVar[str] = ContextVar("arol_request_id", default="")


class Metrics:
    def __init__(self) -> None:
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], dict] = {}
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._lock = Lock()

    def observe(self, name: str, duration_ms: float, labels: dict[str, str] | None = None) -> None:
        normalized_labels = _normalize_labels(labels)
        key = (name, normalized_labels)
        with self._lock:
            histogram = self._histograms.setdefault(
                key,
                {
                    "buckets": {bucket: 0 for bucket in HISTOGRAM_BUCKETS},
                    "count": 0,
                    "sum": 0.0,
                },
            )
            duration = max(0.0, float(duration_ms))
            histogram["count"] += 1
            histogram["sum"] += duration
            for bucket in HISTOGRAM_BUCKETS:
                if duration <= bucket:
                    histogram["buckets"][bucket] += 1

    def increment(self, name: str, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._counters[(name, _normalize_labels(labels))] += 1

    def render(self) -> str:
        with self._lock:
            histograms = {
                key: {
                    "buckets": dict(value["buckets"]),
                    "count": value["count"],
                    "sum": value["sum"],
                }
                for key, value in self._histograms.items()
            }
            counters = dict(self._counters)

        lines: list[str] = []
        metric_names = {name for name, _ in histograms} | {name for name, _ in counters}
        for name in sorted(metric_names):
            if any(metric_name == name for metric_name, _ in histograms):
                lines.append(f"# HELP {name} AI service timing in milliseconds.")
                lines.append(f"# TYPE {name} histogram")
                for (metric_name, labels), histogram in histograms.items():
                    if metric_name != name:
                        continue
                    for bucket in HISTOGRAM_BUCKETS:
                        lines.append(
                            _metric_line(
                                f"{name}_bucket",
                                labels,
                                histogram["buckets"][bucket],
                                {"le": str(bucket)},
                            )
                        )
                    lines.append(
                        _metric_line(f"{name}_bucket", labels, histogram["count"], {"le": "+Inf"})
                    )
                    lines.append(_metric_line(f"{name}_count", labels, histogram["count"]))
                    lines.append(_metric_line(f"{name}_sum", labels, histogram["sum"]))
            else:
                lines.append(f"# HELP {name} AI service counter.")
                lines.append(f"# TYPE {name} counter")
                for (metric_name, labels), value in counters.items():
                    if metric_name == name:
                        lines.append(_metric_line(name, labels, value))
        return "\n".join(lines) + ("\n" if lines else "")


metrics = Metrics()


def set_request_id(value: str):
    return _request_id.set(value)


def reset_request_id(token) -> None:
    _request_id.reset(token)


@contextmanager
def timed(name: str, labels: dict[str, str] | None = None) -> Iterator[None]:
    started = perf_counter()
    try:
        yield
    finally:
        metrics.observe(name, (perf_counter() - started) * 1000, labels)


def _normalize_labels(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))


def _metric_line(
    name: str,
    labels: tuple[tuple[str, str], ...],
    value: float | int,
    extra_labels: dict[str, str] | None = None,
) -> str:
    all_labels = list(labels) + list(_normalize_labels(extra_labels))
    label_text = ",".join(f'{key}="{_escape_label_value(value)}"' for key, value in all_labels)
    return f"{name}{{{label_text}}} {value}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
