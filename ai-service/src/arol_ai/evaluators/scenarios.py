"""Requirement-level scenario checks for the AROL platform.

A scenario is one question, asked as one named person from the fleet dataset,
plus what the answer has to contain for it to count as correct. The suite is the
acceptance definition for the project: every machine, company, user, alarm code
and quotation in it is real data from the supplied workbook, so a scenario that
passes is evidence about the delivered system rather than about a fixture.

Two properties of the schema are deliberate.

**Every scenario names who is asking.** ``access`` is required, and ``null``
means "nobody signed in". The platform fails closed, so an identity left out by
accident would produce refusals that look like retrieval failures; requiring the
key makes the anonymous case a choice rather than an oversight.

**Every scenario declares what it needs.** A runner that cannot supply a
capability in ``requires`` skips those scenarios and reports the skips, rather
than publishing a pass rate over a suite it quietly truncated.

Fields
------

Required: ``id``, ``machineId``, ``message``, ``access``.
Optional, and all additive — a scenario asserts only what it is about:

============================== =================================================
``note``                       Free text; what the scenario is for.
``requires``                   Capabilities: manuals, telemetry, business.
``expectedIntents``            Intents that must appear in the routing.
``expectedAgents``             Agents that must appear in the trace.
``requiredEvidenceSources``    Sources the answer must be grounded in.
``forbiddenEvidenceSources``   Sources it must *not* carry.
``requiredEvidenceMetadata``   Field to accepted values; a list means "any of".
``requiredCitationManual``     Every manual citation must come from this file.
``requiredToolCalls``          Tools that must have run.
``forbiddenToolCalls``         Tools that must not have run at all.
``requiredAnswerContains``     Substrings, matched case-insensitively.
``forbiddenAnswerContains``    Substrings that must be absent.
``requiredAnswerMatches``      Regular expressions.
``requiredAccessDenials``      ``[{"domain": ..., "reason": ...}]``.
``forbidAccessDenials``        True when nothing may be refused.
``minimumDiagnosticSteps``     Lower bound on the diagnostic path.
``requiredDiagnosticStepIds``  Steps that must be present.
``requiredDiagnosticBranches`` Step id to expected pass/fail successor.
``requiredReviewReasons``      Reasons the turn must be flagged for review.
``safetyBlocked``              Whether the guardrail must stop the turn.
============================== =================================================
"""

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from arol_ai.access import ANONYMOUS, AccessContext
from arol_ai.graph.orchestrator import Orchestrator

Scenario = dict[str, Any]

#: What a scenario can ask its runner for. The offline benchmark reads the
#: dataset and the manual PDFs directly; the Compose run reaches the same data
#: through Qdrant, the telemetry service and Business MCP.
CAPABILITIES = frozenset({"manuals", "telemetry", "business"})

REQUIRED_FIELDS = ("id", "machineId", "message", "access")


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    passed: bool
    failures: list[str]
    response: dict
    skipped_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None

    def to_dict(self, *, include_response: bool = False) -> dict:
        payload: dict[str, Any] = {
            "id": self.scenario_id,
            "passed": self.passed,
            "failures": self.failures,
        }
        if self.skipped_reason:
            payload["skipped"] = self.skipped_reason
        if include_response:
            payload["response"] = self.response
        return payload


def load_scenarios(path: Path) -> list[Scenario]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON array of scenarios.")

    scenarios = [
        _validate_scenario(item, path=path, index=index) for index, item in enumerate(payload)
    ]
    _reject_duplicate_ids(scenarios, path=path)
    return scenarios


def scenario_capabilities(scenario: Scenario) -> frozenset[str]:
    return frozenset(scenario.get("requires") or ())


def missing_capabilities(scenario: Scenario, available: frozenset[str] | None) -> frozenset[str]:
    """Capabilities the scenario needs that the runner cannot supply."""
    if available is None:
        return frozenset()

    return scenario_capabilities(scenario) - available


def evaluate_scenarios(
    scenarios: list[Scenario],
    *,
    orchestrator: Orchestrator | None = None,
    capabilities: frozenset[str] | None = None,
) -> list[ScenarioResult]:
    runner = orchestrator or Orchestrator()
    results: list[ScenarioResult] = []

    for scenario in scenarios:
        scenario_id = scenario["id"]
        unavailable = missing_capabilities(scenario, capabilities)
        if unavailable:
            results.append(
                ScenarioResult(
                    scenario_id=scenario_id,
                    passed=True,
                    failures=[],
                    response={},
                    skipped_reason=f"requires {', '.join(sorted(unavailable))}",
                )
            )
            continue

        try:
            response = runner.complete_chat(scenario_request(scenario))
            failures = evaluate_response(scenario, response)
        except Exception as exc:
            response = {}
            failures = [f"scenario raised {type(exc).__name__}: {exc}"]

        results.append(
            ScenarioResult(
                scenario_id=scenario_id,
                passed=not failures,
                failures=failures,
                response=response,
            )
        )

    return results


def evaluate_response(scenario: Scenario, response: dict) -> list[str]:
    failures: list[str] = []
    failures.extend(
        _missing_expected_items(
            label="intent",
            expected=scenario.get("expectedIntents", []),
            actual=response.get("intents", []),
        )
    )
    failures.extend(
        _missing_expected_items(
            label="agent",
            expected=scenario.get("expectedAgents", []),
            actual=response.get("agentTrace", []),
        )
    )
    failures.extend(
        _missing_expected_items(
            label="evidence source",
            expected=scenario.get("requiredEvidenceSources", []),
            actual=[item.get("source") for item in response.get("evidence", [])],
        )
    )
    failures.extend(_forbidden_evidence_failures(scenario, response))
    failures.extend(_metadata_failures(scenario, response))
    failures.extend(_safety_failures(scenario, response))
    failures.extend(_answer_text_failures(scenario, response))
    failures.extend(_diagnostic_step_failures(scenario, response))
    failures.extend(_review_reason_failures(scenario, response))
    failures.extend(_access_denial_failures(scenario, response))
    failures.extend(_tool_call_failures(scenario, response))
    failures.extend(_citation_failures(scenario, response))
    return failures


def citation_measurement(scenario: Scenario, response: dict) -> tuple[int, int]:
    """Whether this answer's manual grounding is actually citable.

    Returned as ``(complete, total)`` so callers can sum across a run. Only
    scenarios that require manual evidence are counted, because an answer that
    was never meant to quote a manual cannot be missing a citation.

    An answer counts as complete when at least one manual passage carries a page
    number and a source document, and — where the scenario pins one — that
    document is the manual of the machine the question was about. Those are
    exactly the parts an operator needs to turn a claim back into a page they
    can open on the machine in front of them.
    """
    if "manual" not in (scenario.get("requiredEvidenceSources") or []):
        return (0, 0)

    expected_manual = scenario.get("requiredCitationManual")
    citable = [
        item
        for item in _manual_evidence(response)
        if item.get("page") and item.get("sourceUri")
        if not expected_manual or expected_manual in str(item.get("sourceUri"))
    ]
    return (1 if citable else 0, 1)


def scenario_request(scenario: Scenario) -> dict:
    return {
        "sessionId": f"eval-{scenario['id']}-{uuid4().hex}",
        "machineId": scenario["machineId"],
        "message": scenario["message"],
        "messages": [],
        "access": access_context(scenario),
    }


def access_context(scenario: Scenario) -> AccessContext:
    """Build the identity a scenario asks as.

    ``"access": null`` means nobody signed in, which is a case the suite tests
    rather than an omission.
    """
    payload = scenario.get("access")
    if not payload:
        return ANONYMOUS

    return AccessContext(
        user_id=payload.get("userId"),
        company_id=payload.get("companyId"),
        visibility=payload.get("visibility"),
        is_staff=bool(payload.get("isStaff")),
        auth_disabled=bool(payload.get("authDisabled")),
    )


def _validate_scenario(payload: Any, *, path: Path, index: int) -> Scenario:
    if not isinstance(payload, dict):
        raise ValueError(f"{path} scenario #{index + 1} must be an object.")

    missing = [
        field
        for field in REQUIRED_FIELDS
        # "access": null is a valid identity, so presence is what matters here.
        if field not in payload or (field != "access" and not payload[field])
    ]
    if missing:
        raise ValueError(f"{path} scenario #{index + 1} missing: {', '.join(missing)}.")

    unknown = scenario_capabilities(payload) - CAPABILITIES
    if unknown:
        raise ValueError(
            f"{path} scenario {payload['id']!r} requires unknown "
            f"capabilities: {', '.join(sorted(unknown))}."
        )

    return payload


def _reject_duplicate_ids(scenarios: list[Scenario], *, path: Path) -> None:
    """Ids name scenarios in every report, so two of them would merge silently."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for scenario in scenarios:
        if scenario["id"] in seen:
            duplicates.add(scenario["id"])
        seen.add(scenario["id"])

    if duplicates:
        raise ValueError(f"{path} has duplicate scenario ids: {', '.join(sorted(duplicates))}.")


def _missing_expected_items(*, label: str, expected: list, actual: list) -> list[str]:
    actual_set = {item for item in actual if item is not None}
    return [f"missing expected {label}: {item}" for item in expected if item not in actual_set]


def _forbidden_evidence_failures(scenario: Scenario, response: dict) -> list[str]:
    forbidden = set(scenario.get("forbiddenEvidenceSources") or [])
    if not forbidden:
        return []

    present = {item.get("source") for item in response.get("evidence") or []}
    return [
        f"answer carries evidence from a source it should have withheld: {source}"
        for source in sorted(forbidden.intersection(present))
    ]


def _metadata_failures(scenario: Scenario, response: dict) -> list[str]:
    metadata_expectations = scenario.get("requiredEvidenceMetadata") or {}
    if not metadata_expectations:
        return []

    evidence = response.get("evidence") or []
    if any(_evidence_matches_metadata(item, metadata_expectations) for item in evidence):
        return []

    return [
        "no evidence item matched required metadata: "
        f"{json.dumps(metadata_expectations, sort_keys=True)}"
    ]


def _evidence_matches_metadata(evidence: dict, expectations: dict) -> bool:
    return all(
        _metadata_field_matches(evidence.get(field), expected_values)
        for field, expected_values in expectations.items()
    )


def _metadata_field_matches(actual: Any, expected_values: Any) -> bool:
    """A list of expected values means "any of these", for list and scalar alike.

    ``chunkKind`` is a single value and ``topics`` is a list, and a scenario
    author naming two of either means the same thing both times: the passage
    should be one of them.
    """
    expected = _string_set(expected_values)

    if isinstance(actual, list):
        return bool(expected.intersection(_string_set(actual)))

    return str(actual) in expected


def _string_set(values: Any) -> set[str]:
    if isinstance(values, list):
        return {str(item) for item in values}

    return {str(values)}


def _safety_failures(scenario: Scenario, response: dict) -> list[str]:
    if "safetyBlocked" not in scenario:
        return []

    expected = bool(scenario["safetyBlocked"])
    actual = _is_guardrail_response(response)
    if actual == expected:
        return []

    return [f"safetyBlocked mismatch: expected {expected}, got {actual}"]


def _is_guardrail_response(response: dict) -> bool:
    intents = set(response.get("intents") or [])
    agent_trace = response.get("agentTrace") or []
    return bool(intents.intersection({"safety", "prompt-injection"})) and agent_trace == [
        "supervisor"
    ]


def _answer_text_failures(scenario: Scenario, response: dict) -> list[str]:
    content = (response.get("message") or {}).get("content") or ""
    lowered = content.lower()
    failures = []

    for expected in scenario.get("requiredAnswerContains", []):
        if str(expected).lower() not in lowered:
            failures.append(f"answer missing required text: {expected}")

    for forbidden in scenario.get("forbiddenAnswerContains", []):
        if str(forbidden).lower() in lowered:
            failures.append(f"answer contains forbidden text: {forbidden}")

    for pattern in scenario.get("requiredAnswerMatches", []):
        if not re.search(str(pattern), content, re.IGNORECASE):
            failures.append(f"answer does not match required pattern: {pattern}")

    return failures


def _diagnostic_step_failures(scenario: Scenario, response: dict) -> list[str]:
    steps = response.get("diagnosticSteps") or []
    failures: list[str] = []
    expected_count = scenario.get("minimumDiagnosticSteps")
    if expected_count is not None:
        actual_count = len(steps)
        if actual_count < int(expected_count):
            failures.append(
                f"minimumDiagnosticSteps mismatch: expected >= {expected_count}, got {actual_count}"
            )

    step_by_id = {step.get("stepId"): step for step in steps if step.get("stepId")}
    for step_id in scenario.get("requiredDiagnosticStepIds", []):
        if step_id not in step_by_id:
            failures.append(f"missing diagnostic step id: {step_id}")

    branch_expectations = scenario.get("requiredDiagnosticBranches") or {}
    for step_id, expected_branch in branch_expectations.items():
        step = step_by_id.get(step_id)
        if step is None:
            failures.append(f"missing diagnostic branch source step id: {step_id}")
            continue

        for field, expected_value in expected_branch.items():
            actual_value = step.get(field)
            if actual_value != expected_value:
                failures.append(
                    f"diagnostic branch mismatch for {step_id}.{field}: "
                    f"expected {expected_value!r}, got {actual_value!r}"
                )

    return failures


def _review_reason_failures(scenario: Scenario, response: dict) -> list[str]:
    return _missing_expected_items(
        label="review reason",
        expected=scenario.get("requiredReviewReasons", []),
        actual=response.get("reviewReasons", []),
    )


def _access_denial_failures(scenario: Scenario, response: dict) -> list[str]:
    denials = response.get("accessDenials") or []
    failures: list[str] = []

    for expected in scenario.get("requiredAccessDenials", []):
        if not any(_denial_matches(denial, expected) for denial in denials):
            failures.append(
                f"missing expected access denial: {json.dumps(expected, sort_keys=True)}"
            )

    if scenario.get("forbidAccessDenials") and denials:
        failures.append(
            "answer refused data the identity is entitled to: "
            + ", ".join(sorted({str(item.get("reason")) for item in denials}))
        )

    return failures


def _denial_matches(denial: dict, expected: dict) -> bool:
    return all(str(denial.get(field)) == str(value) for field, value in expected.items())


def _tool_call_failures(scenario: Scenario, response: dict) -> list[str]:
    calls = response.get("toolCalls") or []
    names = {str(item.get("name")) for item in calls}
    failures = [
        f"expected tool call did not run: {name}"
        for name in scenario.get("requiredToolCalls", [])
        if name not in names
    ]
    # A refused domain must not reach its tool at all. A tool that reads first
    # and filters afterwards has already produced what it was meant to withhold.
    failures.extend(
        f"tool ran for data the identity may not read: {name}"
        for name in scenario.get("forbiddenToolCalls", [])
        if name in names
    )
    return failures


def _citation_failures(scenario: Scenario, response: dict) -> list[str]:
    expected_manual = scenario.get("requiredCitationManual")
    if not expected_manual:
        return []

    manual_evidence = _manual_evidence(response)
    if not manual_evidence:
        return [f"no manual passage was cited, expected one from {expected_manual}"]

    wrong = sorted(
        {
            str(item.get("sourceUri"))
            for item in manual_evidence
            if expected_manual not in str(item.get("sourceUri"))
        }
    )
    failures = [
        f"citation came from the wrong manual: {uri}, expected {expected_manual}" for uri in wrong
    ]
    if not any(item.get("page") for item in manual_evidence):
        failures.append("cited manual passage carries no page number, so it cannot be looked up")
    return failures


def _manual_evidence(response: dict) -> list[dict]:
    return [item for item in response.get("evidence") or [] if item.get("source") == "manual"]


def _default_scenarios_path() -> Path:
    candidates = (
        Path.cwd() / "evaluations" / "scenarios.json",
        Path(__file__).resolve().parents[3] / "evaluations" / "scenarios.json",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AROL AI service evaluation scenarios.")
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=_default_scenarios_path(),
        help="Path to a scenario JSON file.",
    )
    parser.add_argument(
        "--include-response",
        action="store_true",
        help="Include full orchestrator responses in JSON output.",
    )
    parser.add_argument(
        "--capabilities",
        default=",".join(sorted(CAPABILITIES)),
        help=(
            "Comma-separated capabilities this run can supply. Scenarios needing "
            "anything else are skipped and reported."
        ),
    )
    args = parser.parse_args()

    capabilities = frozenset(item.strip() for item in args.capabilities.split(",") if item.strip())
    results = evaluate_scenarios(
        load_scenarios(args.scenarios),
        capabilities=capabilities,
    )
    executed = [result for result in results if not result.skipped]
    # A run that executed nothing is a failure, not a pass. Skips are reported
    # rather than counted as successes, and a suite skipped in its entirety is
    # exactly the silent truncation this schema exists to prevent.
    passed = bool(executed) and all(result.passed for result in results)
    print(
        json.dumps(
            {
                "passed": passed,
                "total": len(results),
                "executed": len(executed),
                "skipped": len(results) - len(executed),
                "failed": sum(1 for result in results if not result.passed),
                "results": [
                    result.to_dict(include_response=args.include_response) for result in results
                ],
            },
            indent=2,
        )
    )

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
