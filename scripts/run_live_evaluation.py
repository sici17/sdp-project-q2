"""Evaluate the running AROL stack and write reproducible acceptance artifacts."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
AI_SRC = ROOT / "ai-service" / "src"
if str(AI_SRC) not in sys.path:
    sys.path.insert(0, str(AI_SRC))

from arol_ai.evaluators.scenarios import (  # noqa: E402
    citation_measurement,
    evaluate_response,
    load_scenarios,
)


def percentile(values: list[float], requested: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * requested / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


class GatewayRefusal(RuntimeError):
    """A typed refusal from the gateway, which several scenarios expect."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code


class GatewayClient:
    """One signed-in identity's view of the gateway.

    A client is per identity, not per run: the suite asks as thirteen different
    people, and the tenant boundary is one of the things being measured, so the
    cookie jar cannot be shared between them.
    """

    def __init__(self, base_url: str, *, user_id: str | None, demo_auth: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf_token = ""
        self.user_id = user_id
        if demo_auth and user_id:
            auth = self.post("/api/v1/auth/demo/login", {"userId": user_id})
            self.csrf_token = str(auth.get("csrfToken") or "")
            if not self.csrf_token:
                raise RuntimeError("demo login did not return a CSRF token")

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(detail)
            except json.JSONDecodeError:
                body = {}
            raise GatewayRefusal(
                exc.code,
                str(body.get("code") or ""),
                str(body.get("message") or detail[:200]),
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"Could not reach {path}: {exc.reason}") from exc


def expected_gateway_refusal(scenario: dict[str, Any]) -> set[str]:
    """Reason codes for which a refusal at the gateway *is* the correct outcome.

    The AI service states a visibility refusal inside the answer, so those
    scenarios still produce a chat response. The tenant boundary is different:
    the gateway declines the request outright, and never forwards it.
    """
    return {
        str(denial.get("reason"))
        for denial in scenario.get("requiredAccessDenials") or []
        if denial.get("reason") == "machine_not_in_company"
    }


def run_scenario(
    client: GatewayClient,
    scenario: dict[str, Any],
    iteration: int,
) -> dict[str, Any]:
    session_key = f"eval-session-{scenario['id']}-{iteration}-{uuid4().hex}"
    session = client.post(
        "/api/v1/chat/sessions",
        {
            "machineId": scenario["machineId"],
            "idempotencyKey": session_key,
        },
        idempotency_key=session_key,
    )
    idempotency_key = f"eval-{scenario['id']}-{iteration}-{uuid4().hex}"
    started = time.perf_counter()
    response = client.post(
        "/api/v1/chat/messages",
        {
            "sessionId": session["sessionId"],
            "machineId": scenario["machineId"],
            "message": scenario["message"],
            "idempotencyKey": idempotency_key,
            "attachments": [],
        },
        idempotency_key=idempotency_key,
    )
    duration_ms = (time.perf_counter() - started) * 1000
    failures = evaluate_response(scenario, response)
    manual_evidence = [
        item for item in response.get("evidence", []) if item.get("source") == "manual"
    ]
    cited, citable = citation_measurement(scenario, response)
    tool_calls = response.get("toolCalls") or []
    return {
        "id": scenario["id"],
        "iteration": iteration,
        "passed": not failures,
        "failures": failures,
        "durationMs": round(duration_ms, 3),
        "manualEvidenceCount": len(manual_evidence),
        "citedAnswers": cited,
        "manualGroundedAnswers": citable,
        "toolCallCount": len(tool_calls),
        "successfulToolCallCount": sum(
            1 for item in tool_calls if item.get("status") == "ok"
        ),
        "agentTrace": response.get("agentTrace") or [],
    }


def _empty_run(
    scenario: dict[str, Any],
    iteration: int,
    *,
    failures: list[str],
) -> dict[str, Any]:
    """A run that produced no chat response, refused or failed."""
    return {
        "id": scenario["id"],
        "iteration": iteration,
        "passed": not failures,
        "failures": failures,
        "durationMs": 0.0,
        "manualEvidenceCount": 0,
        "citedAnswers": 0,
        "manualGroundedAnswers": 0,
        "toolCallCount": 0,
        "successfulToolCallCount": 0,
        "agentTrace": [],
    }


def build_report(
    scenarios: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    iterations: int,
    gateway_url: str,
) -> dict[str, Any]:
    grouped: list[dict[str, Any]] = []
    for scenario in scenarios:
        scenario_runs = [run for run in runs if run["id"] == scenario["id"]]
        durations = [float(run["durationMs"]) for run in scenario_runs]
        grouped.append(
            {
                "id": scenario["id"],
                "passed": all(run["passed"] for run in scenario_runs),
                "passedRuns": sum(1 for run in scenario_runs if run["passed"]),
                "iterations": len(scenario_runs),
                "meanMs": round(mean(durations), 3),
                "p50Ms": round(percentile(durations, 50), 3),
                "p95Ms": round(percentile(durations, 95), 3),
                "failures": sorted(
                    {
                        failure
                        for run in scenario_runs
                        for failure in run["failures"]
                    }
                ),
            }
        )

    cited = sum(run["citedAnswers"] for run in runs)
    citable = sum(run["manualGroundedAnswers"] for run in runs)
    tool_total = sum(run["toolCallCount"] for run in runs)
    tool_success = sum(run["successfulToolCallCount"] for run in runs)
    safety_ids = {
        scenario["id"]
        for scenario in scenarios
        if "safetyBlocked" in scenario
        and (
            scenario["safetyBlocked"]
            or "safety" in scenario.get("requiredEvidenceMetadata", {}).get("topics", [])
        )
    }
    safety_runs = [run for run in runs if run["id"] in safety_ids]
    durations = [float(run["durationMs"]) for run in runs]

    return {
        "schemaVersion": "arol-live-evaluation/v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "scope": "running Compose stack through the public gateway contract",
        "gatewayUrl": gateway_url,
        "iterationsPerScenario": iterations,
        "scenarioCount": len(scenarios),
        "metrics": {
            "scenarioPassRate": sum(item["passed"] for item in grouped) / len(grouped),
            "runPassRate": sum(run["passed"] for run in runs) / len(runs),
            # None rather than 1.0 when nothing was measured: an unmeasured
            # metric is unknown, not perfect.
            "citationCompleteness": (cited / citable if citable else None),
            "citedAnswers": cited,
            "manualGroundedAnswers": citable,
            "safetyPreservation": (
                sum(run["passed"] for run in safety_runs) / len(safety_runs)
                if safety_runs
                else 1.0
            ),
            "toolCallSuccessRate": tool_success / tool_total if tool_total else 1.0,
            "chatP50Ms": percentile(durations, 50),
            "chatP95Ms": percentile(durations, 95),
        },
        "scenarios": grouped,
        "runs": runs,
        "limitations": [
            "Runs on the supplied synthetic fleet dataset, not a live plant.",
            "Does not represent plant-network latency or customer production load.",
            "Production sign-off still requires AROL-owned identity, IoT, and ERP/CRM endpoints.",
        ],
    }


def _percent(value: float | None) -> str:
    """An unmeasured metric reads as unmeasured, not as a perfect score."""
    return "not measured" if value is None else f"{value:.0%}"


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# AROL Q2 Live MVP Acceptance Benchmark",
        "",
        f"Generated: `{report['generatedAt']}`",
        "",
        "This benchmark exercises the running gateway, in-process sessions, "
        "LangGraph orchestration, MCP boundaries, Qdrant manual retrieval, "
        "the fleet dataset, the access model, and safety guardrails.",
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "| --- | ---: |",
        f"| Scenario pass rate | {metrics['scenarioPassRate']:.0%} |",
        f"| Run pass rate | {metrics['runPassRate']:.0%} |",
        f"| Citation completeness | {_percent(metrics['citationCompleteness'])} "
        f"({metrics['citedAnswers']}/{metrics['manualGroundedAnswers']} "
        "manual-grounded answers) |",
        f"| Safety preservation | {metrics['safetyPreservation']:.0%} |",
        f"| Successful tool calls | {metrics['toolCallSuccessRate']:.0%} |",
        f"| Chat p50 | {metrics['chatP50Ms']:.0f} ms |",
        f"| Chat p95 | {metrics['chatP95Ms']:.0f} ms |",
        "",
        "## Scenario comparison",
        "",
        "| Scenario | Pass | Runs | Mean | p50 | p95 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario in report["scenarios"]:
        lines.append(
            f"| `{scenario['id']}` | {'Yes' if scenario['passed'] else 'No'} | "
            f"{scenario['passedRuns']}/{scenario['iterations']} | "
            f"{scenario['meanMs']:.0f} ms | {scenario['p50Ms']:.0f} ms | "
            f"{scenario['p95Ms']:.0f} ms |"
        )
    lines.extend(
        [
            "",
            "![Live scenario latency comparison](live-latency.svg)",
            "",
            "## Scope and limitations",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(
        [
            "",
            "Run from the repository root after the stack is ready:",
            "",
            "```powershell",
            ".\\ai-service\\.venv\\Scripts\\python.exe scripts\\run_live_evaluation.py",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def render_svg(report: dict[str, Any]) -> str:
    scenarios = report["scenarios"]
    width = 1000
    row_height = 42
    height = 90 + (row_height * len(scenarios))
    plot_x = 330
    plot_width = 560
    maximum = max(1.0, max(item["p95Ms"] for item in scenarios) * 1.15)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,sans-serif;fill:#17202a;font-size:13px}"
        ".title{font-size:18px;font-weight:700}.mean{fill:#8793a1}"
        ".p95{fill:#d61f2c}.axis{font-size:11px;fill:#607080}</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="20" y="28" class="title">Live chat latency by acceptance scenario</text>',
        '<rect x="20" y="45" width="14" height="14" class="mean"/>'
        '<text x="40" y="57">mean</text>',
        '<rect x="100" y="45" width="14" height="14" class="p95"/>'
        '<text x="120" y="57">p95</text>',
    ]
    for index, item in enumerate(scenarios):
        y = 78 + (index * row_height)
        mean_width = item["meanMs"] / maximum * plot_width
        p95_width = item["p95Ms"] / maximum * plot_width
        lines.extend(
            [
                f'<text x="20" y="{y + 17}">{escape(item["id"])}</text>',
                f'<rect x="{plot_x}" y="{y}" width="{mean_width:.2f}" '
                'height="11" class="mean"/>',
                f'<rect x="{plot_x}" y="{y + 15}" width="{p95_width:.2f}" '
                'height="11" class="p95"/>',
                f'<text x="{plot_x + plot_width + 10}" y="{y + 11}" '
                f'class="axis">{item["meanMs"]:.0f} ms</text>',
                f'<text x="{plot_x + plot_width + 10}" y="{y + 26}" '
                f'class="axis">{item["p95Ms"]:.0f} ms</text>',
            ]
        )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=ROOT / "ai-service" / "evaluations" / "scenarios.json",
    )
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument(
        "--demo-auth",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="bootstrap the controlled demo cookie and CSRF token (default: enabled)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "docs" / "evaluation",
    )
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")

    scenarios = load_scenarios(args.scenarios)
    clients: dict[str | None, GatewayClient] = {}
    runs: list[dict[str, Any]] = []
    for scenario in scenarios:
        user_id = (scenario.get("access") or {}).get("userId")
        if user_id not in clients:
            clients[user_id] = GatewayClient(
                args.gateway_url,
                user_id=user_id,
                demo_auth=args.demo_auth,
            )
        client = clients[user_id]
        for iteration in range(1, args.iterations + 1):
            try:
                result = run_scenario(client, scenario, iteration)
            except GatewayRefusal as refusal:
                # A machine outside the caller's company never reaches the AI
                # service: the gateway declines it. For the scenarios that ask
                # for exactly that, the refusal is the expected outcome.
                expected = refusal.code in expected_gateway_refusal(scenario)
                if not expected and refusal.status == 401 and scenario.get("access") is None:
                    expected = True
                result = _empty_run(
                    scenario,
                    iteration,
                    failures=[] if expected else [str(refusal)],
                )
                result["refusedByGateway"] = refusal.code or refusal.status
            except Exception as exc:
                result = _empty_run(
                    scenario,
                    iteration,
                    failures=[f"{type(exc).__name__}: {exc}"],
                )
            runs.append(result)
            state = "pass" if result["passed"] else "fail"
            print(f"[{state}] {scenario['id']} #{iteration}: {result['durationMs']:.0f} ms")

    report = build_report(
        scenarios,
        runs,
        iterations=args.iterations,
        gateway_url=args.gateway_url,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "live-benchmark.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "live-benchmark-report.md").write_text(
        render_markdown(report),
        encoding="utf-8",
    )
    (args.output_dir / "live-latency.svg").write_text(
        render_svg(report),
        encoding="utf-8",
    )
    print(json.dumps(report["metrics"], indent=2))
    return 0 if all(run["passed"] for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
