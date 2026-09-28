"""Offline regression benchmark over the supplied fleet dataset.

Runs the requirement scenario suite through the real LangGraph orchestrator with
no services running: manuals come from the eight supplied PDFs, and telemetry,
alarms, maintenance and commercial records come from the SQLite dataset through
the same queries the MCP servers run. See ``scripts/dataset_harness.py`` for
exactly which parts are real and which are not.

This is the fast regression net, not the headline result. The Compose
acceptance run in CI exercises Qdrant, Ollama and the MCP services over the
network on the same scenarios; that is the number to quote.

Note that the two runs do not share a retriever: this one reads the manual
PDFs through ``ManualLexicalRetriever``, while the live stack answers through
Doc MCP's Qdrant index. A green run here is a regression net, not evidence
about the retrieval the demo actually performs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dataset_harness import available_capabilities, build_orchestrator  # noqa: E402

from arol_ai.evaluators.scenarios import (  # noqa: E402
    citation_measurement,
    evaluate_response,
    load_scenarios,
    missing_capabilities,
    scenario_request,
)


@dataclass(frozen=True)
class ScenarioMeasurement:
    scenario_id: str
    iterations: int
    passed_runs: int
    cold_ms: float
    warm_p50_ms: float
    warm_p95_ms: float
    mean_ms: float
    failures: tuple[str, ...]
    cited: int = 0
    citable: int = 0
    skipped_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None

    @property
    def passed(self) -> bool:
        if self.skipped:
            return True
        return self.passed_runs == self.iterations and not self.failures

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.scenario_id,
            "iterations": self.iterations,
            "passed": self.passed,
            "passedRuns": self.passed_runs,
            "coldMs": round(self.cold_ms, 3),
            "warmP50Ms": round(self.warm_p50_ms, 3),
            "warmP95Ms": round(self.warm_p95_ms, 3),
            "meanMs": round(self.mean_ms, 3),
            "failures": list(self.failures),
        }
        if self.skipped_reason:
            payload["skipped"] = self.skipped_reason
        return payload


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _skipped(scenario_id: str, reason: str, iterations: int) -> ScenarioMeasurement:
    return ScenarioMeasurement(
        scenario_id=scenario_id,
        iterations=iterations,
        passed_runs=0,
        cold_ms=0.0,
        warm_p50_ms=0.0,
        warm_p95_ms=0.0,
        mean_ms=0.0,
        failures=(),
        skipped_reason=reason,
    )


def measure_scenario(
    orchestrator,
    scenario: dict[str, Any],
    iterations: int,
) -> ScenarioMeasurement:
    durations: list[float] = []
    failures: list[str] = []
    passed_runs = 0
    cited = 0
    citable = 0

    for _ in range(iterations):
        request = scenario_request(scenario)
        started = time.perf_counter()
        try:
            response = orchestrator.complete_chat(request)
        except Exception as exc:
            durations.append((time.perf_counter() - started) * 1000)
            failure = f"scenario raised {type(exc).__name__}: {exc}"
            if failure not in failures:
                failures.append(failure)
            continue

        durations.append((time.perf_counter() - started) * 1000)
        run_failures = evaluate_response(scenario, response)
        if not run_failures:
            passed_runs += 1
        for failure in run_failures:
            if failure not in failures:
                failures.append(failure)

        run_cited, run_citable = citation_measurement(scenario, response)
        cited += run_cited
        citable += run_citable

    warm = durations[1:] or durations
    return ScenarioMeasurement(
        scenario_id=str(scenario["id"]),
        iterations=iterations,
        passed_runs=passed_runs,
        cold_ms=durations[0],
        warm_p50_ms=_percentile(warm, 50),
        warm_p95_ms=_percentile(warm, 95),
        mean_ms=mean(durations),
        failures=tuple(failures),
        cited=cited,
        citable=citable,
    )


def _rate(numerator: int, denominator: int) -> float | None:
    """None rather than 1.0 when nothing was measured.

    A metric with an empty denominator is unknown, and reporting it as a perfect
    score is how a benchmark comes to overstate itself.
    """
    return numerator / denominator if denominator else None


def build_report(
    scenarios: list[dict[str, Any]],
    measurements: list[ScenarioMeasurement],
    *,
    iterations: int,
    capabilities: frozenset[str],
) -> dict[str, Any]:
    by_id = {scenario["id"]: scenario for scenario in scenarios}
    executed = [item for item in measurements if not item.skipped]
    skipped = [item for item in measurements if item.skipped]

    def subset(predicate) -> list[ScenarioMeasurement]:
        return [item for item in executed if predicate(by_id[item.scenario_id])]

    safety = subset(lambda scenario: scenario.get("safetyBlocked"))
    access = subset(
        lambda scenario: scenario.get("requiredAccessDenials")
        or scenario.get("forbidAccessDenials")
        or scenario.get("forbiddenToolCalls")
    )
    cited = sum(item.cited for item in executed)
    citable = sum(item.citable for item in executed)

    return {
        "schemaVersion": "arol-evaluation/v2",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "scope": "offline dataset regression benchmark (no services running)",
        "iterationsPerScenario": iterations,
        "scenarioCount": len(measurements),
        "executedScenarioCount": len(executed),
        "capabilities": sorted(capabilities),
        "metrics": {
            "scenarioPassRate": _rate(
                sum(item.passed for item in executed), len(executed)
            ),
            "runPassRate": _rate(
                sum(item.passed_runs for item in executed), len(executed) * iterations
            ),
            # Computed, not asserted: the share of manual-grounded answers that
            # cite a page of the right machine's manual.
            "citationCompleteness": _rate(cited, citable),
            "citedAnswers": cited,
            "manualGroundedAnswers": citable,
            "accessControlPassRate": _rate(
                sum(item.passed for item in access), len(access)
            ),
            "safetyPreservation": _rate(sum(item.passed for item in safety), len(safety)),
            "warmP95Ms": _percentile([item.warm_p95_ms for item in executed], 95),
        },
        "scenarios": [item.to_dict() for item in measurements],
        "skipped": [
            {"id": item.scenario_id, "reason": item.skipped_reason} for item in skipped
        ],
        "failures": [
            failure for item in measurements if not item.passed for failure in item.failures
        ],
        "limitations": [
            "Retrieval here is lexical over the manual PDFs; the deployed stack "
            "is hybrid lexical and vector over Qdrant with Ollama embeddings.",
            "There is no HTTP hop and no separate MCP server process, so latency "
            "is a regression baseline for the graph and not a system measurement.",
            "Answers are composed deterministically. The demo runs Ollama "
            "synthesis over the same grounded draft.",
            "The Compose acceptance run covers what this cannot; it is the "
            "number to quote.",
        ],
    }


def render_svg(measurements: list[ScenarioMeasurement]) -> str:
    executed = [item for item in measurements if not item.skipped]
    if not executed:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="60"></svg>\n'

    width = 1080
    row_height = 34
    height = 100 + row_height * len(executed)
    plot_x = 420
    plot_width = 500
    maximum = max(
        1.0,
        max(max(item.cold_ms, item.warm_p95_ms) for item in executed) * 1.15,
    )
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,sans-serif;fill:#17202a;font-size:12px}"
        ".title{font-size:18px;font-weight:700}.axis{fill:#607080;font-size:10px}"
        ".cold{fill:#b3261e}.warm{fill:#2d7f5e}.fail{fill:#b3261e;font-weight:700}</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="20" y="28" class="title">Scenario latency: cold vs warm p95</text>',
        '<rect x="20" y="45" width="14" height="14" class="cold"/><text x="40" y="57">cold</text>',
        '<rect x="90" y="45" width="14" height="14" class="warm"/>'
        '<text x="110" y="57">warm p95</text>',
        f'<line x1="{plot_x}" y1="72" x2="{plot_x + plot_width}" y2="72" stroke="#c7d0d8"/>',
    ]

    for index, item in enumerate(executed):
        y = 84 + index * row_height
        cold_width = item.cold_ms / maximum * plot_width
        warm_width = item.warm_p95_ms / maximum * plot_width
        label = escape(item.scenario_id)
        style = "" if item.passed else ' class="fail"'
        lines.extend(
            [
                f'<text x="20" y="{y + 12}"{style}>{label}</text>',
                f'<rect x="{plot_x}" y="{y}" width="{cold_width:.2f}" height="8" class="cold"/>',
                f'<rect x="{plot_x}" y="{y + 11}" width="{warm_width:.2f}" height="8" class="warm"/>',
                f'<text x="{plot_x + plot_width + 10}" y="{y + 8}" '
                f'class="axis">{item.cold_ms:.0f} ms</text>',
                f'<text x="{plot_x + plot_width + 10}" y="{y + 19}" '
                f'class="axis">{item.warm_p95_ms:.0f} ms</text>',
            ]
        )

    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _percent(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.0%}"


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    rows = [
        "# AROL Q2 offline dataset benchmark",
        "",
        f"Generated: `{report['generatedAt']}`",
        "",
        "The requirement scenario suite run through the real orchestrator against "
        "the supplied fleet dataset and the eight machine manuals, with no "
        "services running. This is the fast regression net; the Compose "
        "acceptance run is the headline measurement.",
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "| --- | ---: |",
        f"| Scenarios executed | {report['executedScenarioCount']} of {report['scenarioCount']} |",
        f"| Scenario pass rate | {_percent(metrics['scenarioPassRate'])} |",
        f"| Run pass rate | {_percent(metrics['runPassRate'])} |",
        (
            f"| Citation completeness | {_percent(metrics['citationCompleteness'])} "
            f"({metrics['citedAnswers']}/{metrics['manualGroundedAnswers']} "
            "manual-grounded answers) |"
        ),
        f"| Access-control pass rate | {_percent(metrics['accessControlPassRate'])} |",
        f"| Safety preservation | {_percent(metrics['safetyPreservation'])} |",
        f"| Aggregate warm p95 | {metrics['warmP95Ms']:.1f} ms |",
        "",
        "Citation completeness is the share of answers that were required to be "
        "grounded in a manual and cited a page of *that machine's* manual. It is "
        "computed from the run, not declared.",
        "",
        "## Scenarios",
        "",
        "| Scenario | Pass | Cold | Warm p50 | Warm p95 | Mean |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]

    for item in report["scenarios"]:
        if item.get("skipped"):
            rows.append(
                f"| `{item['id']}` | skipped | — | — | — | {item['skipped']} |"
            )
            continue
        rows.append(
            f"| `{item['id']}` | {'Yes' if item['passed'] else 'No'} | "
            f"{item['coldMs']:.0f} ms | {item['warmP50Ms']:.0f} ms | "
            f"{item['warmP95Ms']:.0f} ms | {item['meanMs']:.0f} ms |"
        )

    rows.extend(["", "![Scenario latency comparison](latency.svg)", ""])

    if report["failures"]:
        rows.extend(["## Failures", ""])
        rows.extend(f"- {failure}" for failure in report["failures"])
        rows.append("")

    rows.extend(["## Scope and limitations", ""])
    rows.extend(f"- {limitation}" for limitation in report["limitations"])
    rows.extend(
        [
            "",
            "Run it from the repository root with:",
            "",
            "```bash",
            "python scripts/run_evaluation.py",
            "```",
            "",
            "The JSON data is in `benchmark.json`; the chart is generated without "
            "external plotting dependencies in `latency.svg`.",
            "",
        ]
    )
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=ROOT / "ai-service" / "evaluations" / "scenarios.json",
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs" / "evaluation")
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated scenario ids to run, for iterating on one case.",
    )
    args = parser.parse_args()

    if args.iterations < 2:
        parser.error("--iterations must be at least 2 to compare cold and warm latency")

    scenarios = load_scenarios(args.scenarios)
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        scenarios = [scenario for scenario in scenarios if scenario["id"] in wanted]

    capabilities = available_capabilities()
    orchestrator = build_orchestrator()
    measurements: list[ScenarioMeasurement] = []
    for scenario in scenarios:
        unavailable = missing_capabilities(scenario, capabilities)
        if unavailable:
            measurements.append(
                _skipped(
                    scenario["id"],
                    f"requires {', '.join(sorted(unavailable))}",
                    args.iterations,
                )
            )
            continue
        measurements.append(measure_scenario(orchestrator, scenario, args.iterations))

    report = build_report(
        scenarios,
        measurements,
        iterations=args.iterations,
        capabilities=capabilities,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "benchmark.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "benchmark-report.md").write_text(
        render_markdown(report),
        encoding="utf-8",
    )
    (args.output_dir / "latency.svg").write_text(
        render_svg(measurements),
        encoding="utf-8",
    )

    for item in measurements:
        state = "skip" if item.skipped else ("pass" if item.passed else "FAIL")
        print(f"[{state}] {item.scenario_id}")
        for failure in item.failures:
            print(f"         {failure}")
    print(json.dumps(report["metrics"], indent=2))

    # Executing nothing is a failure. A suite skipped in its entirety would
    # otherwise report a clean run over no evidence at all.
    executed = [item for item in measurements if not item.skipped]
    return 0 if executed and all(item.passed for item in measurements) else 1


if __name__ == "__main__":
    raise SystemExit(main())
