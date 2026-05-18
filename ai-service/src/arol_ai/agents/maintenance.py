"""Machine-specific maintenance intervals with auditable, bounded hour estimates."""

import re
from functools import lru_cache
from pathlib import Path

import fitz

from arol_ai.config import platform_today
from arol_ai.graph.state import Evidence


def maintenance_due_requested(query: str) -> bool:
    return bool(
        re.search(r"\b(maintenance|service)\b", query, re.I)
        and re.search(r"\b(due|periodic|next|overdue|when)\b", query, re.I)
    )


@lru_cache(maxsize=32)
def read_schedule(path: str, modified: int) -> tuple[dict, ...]:
    """Extract actual interval sections, excluding the table of contents."""
    intervals = {}
    with fitz.open(path) as document:
        for index, page in enumerate(document):
            text = page.get_text()
            if text.count("....") > 2 or re.search(r"(?:\.\s*){8}", text):
                continue
            matches = list(
                re.finditer(
                    r"\bEVERY\s+([\d, ]+)\s+(?:(?:WORKING|OPERATING)\s+)?HOURS\b", text, re.I
                )
            )
            for i, match in enumerate(matches):
                hours = int(re.sub(r"\D", "", match.group(1)))
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                passage = text[match.start() : end].strip()
                if re.match(r"\s*(?:or\b|[.*])", text[match.end() : end], re.I):
                    continue
                if len(passage) < 80 or hours in intervals:
                    continue
                lines = [line.strip() for line in text[match.end() : end].splitlines()]
                lines = [
                    line
                    for line in lines
                    if line
                    and line
                    not in {
                        "OPERATION",
                        "AIM",
                        "NOTES",
                        "NOTE",
                        "PURPOSE",
                        "LUBRICATION POINTS",
                        "DESCRIPTION",
                        "lubrication",
                    }
                    and not re.fullmatch(r"[\d.]+", line)
                ]
                if not lines:
                    continue
                action = next(
                    (
                        i
                        for i, line in enumerate(lines)
                        if re.match(
                            r"(?:check|clean|lubricat|overhaul|replace|air pressure|pneumatic|container|closure|head|electric)",  # noqa: E501 - one regex reads worse split across concatenation
                            line,
                            re.I,
                        )
                    ),
                    None,
                )
                if action is None:
                    continue
                lines = lines[action:]
                task = re.split(r"(?<=\.)\s", " ".join(lines), maxsplit=1)[0]
                intervals[hours] = dict(
                    hours=hours, page=index + 1, task=task[:220], excerpt=passage[:1600]
                )
    return tuple(intervals[key] for key in sorted(intervals))


def assess_maintenance(
    manual_path: Path, manual_url: str, rows: list[dict], tickets: list
) -> tuple[list[str], list[Evidence]]:
    schedule = read_schedule(str(manual_path), manual_path.stat().st_mtime_ns)
    today = platform_today().isoformat()
    # Each dataset row describes ONE hour. Uptime is productive fraction, not
    # machine age or a lifetime service-hour counter. Never count future rows.
    rows = sorted(
        (row for row in rows if str(row.get("timestamp", ""))[:10] < today),
        key=lambda row: row.get("timestamp", ""),
    )
    productive = sum(
        max(0, min(100, float(row.get("uptimePercentage") or 0))) / 100 for row in rows
    )
    scheduled = [t for t in tickets if t.ticket_type == "Scheduled maintenance"]
    last = max((t.created_date for t in scheduled if t.created_date), default=None)
    bullets = [
        f"Reference date: {today}.",
        f"Observed productive hours: {productive:.1f} h = sum(uptimePercentage / 100) across {len(rows)} hourly snapshots.",  # noqa: E501 - one regex reads worse split across concatenation
    ]
    if rows:
        bullets.append(f"Observation window: {rows[0]['timestamp']} to {rows[-1]['timestamp']}.")
    if last:
        bullets.append(
            f"Last scheduled maintenance ticket: {last}. This is its creation date, not proof that every maintenance task was completed."  # noqa: E501 - one regex reads worse split across concatenation
        )
    bullets.append(
        "The dataset has no lifetime hour meter or task-specific completion log. Productive "
        "hours are a lower-bound working-hour estimate; do not reset every interval from a "
        "generic ticket."
    )
    evidence = []
    for item in schedule:
        hours = item["hours"]
        if not rows:
            status = "Due status unknown: telemetry history is unavailable."
        elif productive >= hours:
            status = f"Threshold reached in the observed window ({productive:.1f} >= {hours} h); check the task log and perform it if not already completed."  # noqa: E501 - one regex reads worse split across concatenation
        else:
            status = f"{hours - productive:.1f} productive hours remain to the first observed-window threshold; lifetime due status is unknown."  # noqa: E501 - one regex reads worse split across concatenation
        bullets.append(
            f"Every {hours} working hours: tasks include {item['task']} {status} See manual PDF page {item['page']} for the complete interval task list."  # noqa: E501 - one regex reads worse split across concatenation
        )
        evidence.append(
            Evidence(
                source="manual",
                title=f"Maintenance every {hours} working hours",
                excerpt=item["excerpt"],
                page=item["page"],
                source_uri=manual_url,
                confidence=1.0,
                section="Scheduled maintenance",
            )
        )
    if not schedule:
        bullets.append(
            "No unambiguous hourly schedule was extracted. Consult the machine manual before deciding what is due."
        )
    bullets.append(
        "A calendar due date requires the actual operating schedule and the last completion of "
        "each task; neither is recorded. Qualified maintenance personnel must follow the cited "
        "isolation and safety procedures."
    )
    return bullets, evidence
