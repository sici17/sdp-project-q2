"""Compare local chat models on the one job they have here: writing a grounded draft.

The platform does not ask a model to find anything. Retrieval, the dataset
queries, the access checks and the answer's content are all deterministic, and
what reaches the model is a finished draft plus the evidence it was built from.
The model's job is to write that well without changing what it says.

So the comparison is not "which model is smarter". It is:

* **Fidelity** — does every figure and identifier in the answer come from the
  draft? A model that invents a page number or a price is unusable here at any
  size, because the whole claim of the system is that answers are traceable.
* **Retention** — does the answer keep the identifiers, citations and quantities
  the draft carried? Losing them is quieter than inventing them and just as bad:
  the answer still reads well, but the operator can no longer check it.
* **Refusal retention** — when the draft withholds a domain the user may not
  read, does the answer still say so? A refusal smoothed into silence reads as
  "there is nothing to report", which is the one meaning it must never have.
* **Latency** — a phone on a plant floor.

Run it against a live Ollama with the models already pulled:

    python scripts/compare_llm_models.py --models qwen2.5:0.5b-instruct,qwen2.5:1.5b-instruct
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dataset_harness import build_orchestrator  # noqa: E402

from arol_ai.evaluators.scenarios import load_scenarios, scenario_request  # noqa: E402
from arol_ai.llm.providers import LLMProviderError, OllamaChatProvider  # noqa: E402

#: The scenarios worth judging synthesis on: one grounded in a manual, one
#: commercial with figures that must survive, one telemetry readout, and one
#: partial refusal.
DEFAULT_SCENARIO_IDS = (
    "brief-2-alarm-meaning-al017",
    "brief-5-latest-quote-revision",
    "brief-6-delivery-and-cost",
    "access-technician-allowed-alarms",
    "access-commercial-denied-telemetry",
)

#: Identifiers and figures an answer must not invent and should not lose.
#: Alarm codes, quote/order/ticket ids, dates, page numbers and money.
#:
#: Single digits are excluded deliberately. In these answers they are almost
#: always list ordinals, and counting "3." in a numbered step as a fabricated
#: figure would bury the inventions that matter under formatting noise.
_TOKEN = re.compile(
    r"\b(?:AL\d{3}_[A-Z0-9_]+|QTE-\d{4}-\d{4}|ORD-\d{4}-\d{4}|TCK-\d{4}"
    r"|\d{4}-\d{2}-\d{2}|\d[\d,]*\.\d{2}|\d{2,})\b"
)

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_REFUSAL_MARKERS = ("not available to you", "does not include", "cannot answer")


def _tokens(text: str) -> set[str]:
    return {match.group(0) for match in _TOKEN.finditer(text)}


def _grounding_corpus(context: dict) -> set[str]:
    """Everything the model was given, as tokens it is allowed to reuse.

    ISO dates are also expanded into their parts, because a model that writes
    ``2026-05-14`` as "May 14, 2026" has reformatted a supplied fact, not
    invented one. Without this the measure punishes good prose and the real
    fabrications get lost in the count.
    """
    serialized = json.dumps(context, ensure_ascii=True, sort_keys=True)
    allowed = _tokens(serialized)
    for year, month, day in _ISO_DATE.findall(serialized):
        allowed.update({year, month, day, str(int(month)), str(int(day))})
    return allowed


def _score(answer: str, *, draft: str, context: dict) -> dict[str, Any]:
    allowed = _grounding_corpus(context)
    answer_tokens = _tokens(answer)
    draft_tokens = _tokens(draft)

    invented = sorted(answer_tokens - allowed)
    kept = draft_tokens.intersection(answer_tokens)
    draft_refuses = any(marker in draft.lower() for marker in _REFUSAL_MARKERS)
    answer_refuses = any(marker in answer.lower() for marker in _REFUSAL_MARKERS)

    return {
        "invented": invented,
        "inventedCount": len(invented),
        "retention": len(kept) / len(draft_tokens) if draft_tokens else None,
        "refusalExpected": draft_refuses,
        "refusalKept": (answer_refuses if draft_refuses else None),
        "characters": len(answer),
    }


def _drafts(scenario_ids: tuple[str, ...], scenarios_path: Path) -> list[dict[str, Any]]:
    """Produce the grounded draft for each scenario, once, deterministically."""
    by_id = {scenario["id"]: scenario for scenario in load_scenarios(scenarios_path)}
    missing = [item for item in scenario_ids if item not in by_id]
    if missing:
        raise SystemExit(f"unknown scenario id(s): {', '.join(missing)}")

    orchestrator = build_orchestrator()
    drafts = []
    for scenario_id in scenario_ids:
        scenario = by_id[scenario_id]
        response = orchestrator.complete_chat(scenario_request(scenario))
        # complete_chat with the deterministic provider returns the draft as the
        # answer, and stashes the synthesis context the provider would receive.
        drafts.append(
            {
                "id": scenario_id,
                "message": scenario["message"],
                "draft": response["message"]["content"],
                "context": _last_answer_context(orchestrator, scenario),
            }
        )
    return drafts


def _last_answer_context(orchestrator, scenario: dict) -> dict:
    """Re-run the turn to capture the exact context a provider would be sent."""
    state = orchestrator._run_graph(scenario_request(scenario))
    return state.answer_context or {"draft": state.answer}


def _run_once(provider, draft: dict, *, model: str, attempt: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        answer = provider.synthesize(draft["context"])
        error = None
    except LLMProviderError as exc:
        answer, error = "", str(exc)
    elapsed = (time.perf_counter() - started) * 1000

    record: dict[str, Any] = {
        "model": model,
        "scenario": draft["id"],
        "run": attempt,
        "latencyMs": round(elapsed, 1),
        "error": error,
        "answer": answer,
    }
    record.update(
        _score(answer, draft=draft["draft"], context=draft["context"])
        if answer
        else {"invented": [], "inventedCount": 0, "retention": 0.0}
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models",
        default="qwen2.5:0.5b-instruct,qwen2.5:1.5b-instruct",
        help="Comma-separated Ollama model tags, already pulled.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds per synthesis. Generous, because this runs on CPU.",
    )
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=ROOT / "ai-service" / "evaluations" / "scenarios.json",
    )
    parser.add_argument("--only", default=",".join(DEFAULT_SCENARIO_IDS))
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="Runs per model per scenario. Local models vary between runs even "
        "at temperature 0, so one sample is not a comparison.",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs" / "evaluation")
    args = parser.parse_args()

    scenario_ids = tuple(item.strip() for item in args.only.split(",") if item.strip())
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    drafts = _drafts(scenario_ids, args.scenarios)

    results: list[dict[str, Any]] = []
    for model in models:
        provider = OllamaChatProvider(
            base_url=args.base_url,
            model=model,
            timeout_seconds=args.timeout,
        )
        for draft in drafts:
            for attempt in range(1, args.repeats + 1):
                record = _run_once(provider, draft, model=model, attempt=attempt)
                results.append(record)
                print(
                    f"[{model}] {draft['id']} #{attempt}: "
                    f"{record['latencyMs']:.0f} ms, "
                    f"{record['inventedCount']} invented, "
                    f"retention {record.get('retention') or 0:.0%}"
                    + (f", ERROR {record['error']}" if record["error"] else "")
                )

    summary = []
    for model in models:
        rows = [item for item in results if item["model"] == model]
        refusal_rows = [item for item in rows if item.get("refusalExpected")]
        summary.append(
            {
                "model": model,
                "runs": len(rows),
                "errors": sum(1 for item in rows if item["error"]),
                "meanLatencyMs": round(mean(item["latencyMs"] for item in rows), 1),
                "totalInvented": sum(item["inventedCount"] for item in rows),
                "meanRetention": round(
                    mean(item.get("retention") or 0.0 for item in rows), 3
                ),
                "refusalsKept": sum(1 for item in refusal_rows if item.get("refusalKept")),
                "refusalsExpected": len(refusal_rows),
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "llm-model-comparison.json").write_text(
        json.dumps({"summary": summary, "runs": results}, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "llm-model-comparison.md").write_text(
        render_markdown(summary, results, scenario_ids=scenario_ids),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    return 0


def render_markdown(
    summary: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    scenario_ids: tuple[str, ...],
) -> str:
    lines = [
        "# Local chat model comparison",
        "",
        "Which model should write the demo's answers. The model does not find "
        "anything: retrieval, the dataset queries, the access checks and the "
        "answer's content are deterministic, and what reaches the model is a "
        "finished grounded draft. So this measures whether a model can write "
        "that draft without changing what it says.",
        "",
        "| Measure | What it means |",
        "| --- | --- |",
        "| Invented | Figures or identifiers in the answer that were in neither "
        "the draft nor the evidence. Any number above zero disqualifies a model: "
        "the platform's claim is that answers are traceable. Reformatted dates "
        "do not count - a model writing `2026-05-14` as *May 14, 2026* has "
        "restated a supplied fact - and single digits are ignored because in "
        "these answers they are list ordinals. |",
        "| Retention | Share of the draft's identifiers, dates, page numbers and "
        "amounts that survive into the answer. Losing them is quieter than "
        "inventing them and just as damaging - the answer still reads well, but "
        "it can no longer be checked. |",
        "| Refusal kept | When the draft withholds a domain the user may not "
        "read, does the answer still say so. A refusal smoothed into silence "
        "reads as *there is nothing to report*. |",
        "| Latency | Wall clock for one synthesis, CPU only. |",
        "",
        "**What this cannot see.** Retention and invention are token measures. A "
        "model that picks a real figure from the context and attaches it to the "
        "wrong label scores clean on both, so the answers themselves were read. "
        "That review is what the notes below record.",
        "",
        "## Results",
        "",
        "| Model | Runs | Errors | Invented | Retention | Refusals kept | Mean latency |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summary:
        refusals = (
            f"{item['refusalsKept']}/{item['refusalsExpected']}"
            if item["refusalsExpected"]
            else "n/a"
        )
        lines.append(
            f"| `{item['model']}` | {item['runs']} | {item['errors']} | "
            f"{item['totalInvented']} | {item['meanRetention']:.0%} | {refusals} | "
            f"{item['meanLatencyMs'] / 1000:.1f} s |"
        )

    lines.extend(
        [
            "",
            "## Per scenario",
            "",
            "| Model | Scenario | Latency | Invented | Retention |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for item in results:
        retention = item.get("retention")
        lines.append(
            f"| `{item['model']}` | `{item['scenario']}` | "
            f"{item['latencyMs'] / 1000:.1f} s | {item['inventedCount']} | "
            + ("n/a" if retention is None else f"{retention:.0%}")
            + " |"
        )

    lines.extend(
        [
            "",
            "Scenarios used: " + ", ".join(f"`{item}`" for item in scenario_ids) + ".",
            "",
            "Reproduce with:",
            "",
            "```bash",
            "docker compose -f infrastructure/docker-compose.yml up -d ollama",
            "docker exec infrastructure-ollama-1 ollama pull qwen2.5:1.5b-instruct",
            "python scripts/compare_llm_models.py",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
