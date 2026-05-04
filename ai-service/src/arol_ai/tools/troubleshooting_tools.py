from dataclasses import dataclass

from arol_ai.domain.models import TelemetrySnapshot, describe_reading
from arol_ai.graph.evidence_selection import select_manual_evidence
from arol_ai.graph.state import DiagnosticStep, Evidence
from arol_ai.rag.text import (
    focus_alarm_passage,
    readable_passage,
)
from arol_ai.tools.base import ToolCallRecord

ALARM_QUERY_TERMS = {
    "alarm",
    "alarms",
    "code",
    "error",
    "fault",
    "message",
    "troubleshooting",
}
PROCEDURAL_QUERY_TERMS = {
    "adjust",
    "check",
    "confirm",
    "inspect",
    "procedure",
    "verify",
    "what",
}
SAFETY_QUERY_TERMS = {"door", "emergency", "guard", "interlock", "safety", "stop", "voltage"}
STEP_RECORD_ALARM = "record-active-alarm"
STEP_PRESERVE_SAFETY = "preserve-safety-protections"
STEP_MANUAL_CHECK = "manual-check"
STEP_TORQUE_PATH = "torque-path-check"
STEP_HEAT_FRICTION = "heat-friction-check"
STEP_ESCALATE_SAFETY = "escalate-safety-diagnostics"
STEP_ESCALATE_PERSISTENT = "escalate-persistent-alarm"


@dataclass(frozen=True)
class TroubleshootingResult:
    diagnostic_steps: list[DiagnosticStep]
    tool_call: ToolCallRecord


class TroubleshootingTool:
    name = "troubleshooting.diagnose"
    agent = "troubleshooting-agent"

    def run(
        self,
        *,
        query: str,
        telemetry: TelemetrySnapshot | None,
        evidence: list[Evidence],
    ) -> TroubleshootingResult:
        steps = _diagnostic_steps(query=query, telemetry=telemetry, evidence=evidence)
        status = "ok" if steps else "empty"
        output_summary = (
            f"Returned {len(steps)} guided diagnostic step(s)."
            if steps
            else "No diagnostic steps could be produced from available context."
        )

        return TroubleshootingResult(
            diagnostic_steps=steps,
            tool_call=ToolCallRecord.create(
                name=self.name,
                agent=self.agent,
                status=status,
                input_summary=f"guided troubleshooting for: {query[:160]}",
                output_summary=output_summary,
            ),
        )


def _diagnostic_steps(
    *,
    query: str,
    telemetry: TelemetrySnapshot | None,
    evidence: list[Evidence],
) -> list[DiagnosticStep]:
    # Shared with the answer's citation, so the checklist points at the
    # section the operator was just told to read.
    manual_evidence = select_manual_evidence(evidence, query)
    telemetry_evidence = next((item for item in evidence if item.source == "telemetry"), None)
    safety_context = _is_safety_context(query=query, telemetry=telemetry, evidence=manual_evidence)
    torque_context = _is_torque_context(query=query, telemetry=telemetry)
    steps: list[DiagnosticStep] = []

    if telemetry and telemetry.has_active_alarm:
        steps.append(
            DiagnosticStep(
                label=f"Record active alarm {telemetry.active_alarm}",
                detail=(
                    f"Capture {describe_reading(telemetry)}, "
                    f"and the timestamp {telemetry.timestamp}."
                ),
                priority="immediate",
                requires_technician=False,
                source="telemetry",
                evidence_title=telemetry_evidence.title if telemetry_evidence else None,
                expected_outcome="The active alarm code and telemetry snapshot are recorded.",
                pass_follow_up=f"I recorded active alarm {telemetry.active_alarm}.",
                fail_follow_up=f"I cannot record active alarm {telemetry.active_alarm}.",
                safety_level="operator",
                required_role="operator",
                step_id=STEP_RECORD_ALARM,
                pass_next_step_id=_next_after_record_alarm(
                    safety_context=safety_context,
                    manual_evidence=manual_evidence,
                    torque_context=torque_context,
                    telemetry=telemetry,
                ),
                fail_next_step_id=(
                    STEP_ESCALATE_SAFETY if safety_context else STEP_ESCALATE_PERSISTENT
                ),
            )
        )

    if safety_context:
        steps.append(
            DiagnosticStep(
                label="Stop and preserve safety protections",
                detail=(
                    "Keep guards, interlocks, emergency stops, and safety circuits enabled. "
                    "Do not bypass devices or enter guarded areas while the fault is active. "
                    "Do not reset until the triggering condition is corrected, every affected "
                    "door or guard is secured, the guarded area is clear, and the safety devices "
                    "are confirmed operational."
                ),
                priority="immediate",
                requires_technician=False,
                source="troubleshooting",
                evidence_title=manual_evidence.title if manual_evidence else None,
                page=manual_evidence.page if manual_evidence else None,
                expected_outcome=(
                    "The machine is stopped, the triggering condition is corrected, the area is "
                    "clear, and all safety protections remain enabled before reset."
                ),
                pass_follow_up="The machine is stopped and safety protections remain enabled.",
                fail_follow_up="I cannot confirm the machine is stopped with protections enabled.",
                safety_level="safety-critical",
                required_role="operator",
                step_id=STEP_PRESERVE_SAFETY,
                pass_next_step_id=STEP_MANUAL_CHECK if manual_evidence else STEP_ESCALATE_SAFETY,
                fail_next_step_id=STEP_ESCALATE_SAFETY,
            )
        )

    if manual_evidence:
        manual_safety_level = _manual_safety_level(manual_evidence, safety_context)
        manual_requires_technician = _manual_requires_technician(manual_evidence, safety_context)
        steps.append(
            DiagnosticStep(
                label=f"Follow manual section: {_manual_label(manual_evidence)}",
                detail=_manual_detail(manual_evidence.excerpt, query),
                priority="next",
                requires_technician=manual_requires_technician,
                source="manual",
                evidence_title=manual_evidence.title,
                page=manual_evidence.page,
                expected_outcome=_manual_expected_outcome(manual_evidence, safety_context),
                pass_follow_up=(
                    f"I completed the cited manual check: {_manual_label(manual_evidence)}."
                ),
                fail_follow_up=(
                    f"I checked the cited manual section {_manual_label(manual_evidence)!r} "
                    "and the issue is still present."
                ),
                safety_level=manual_safety_level,
                required_role="technician" if manual_requires_technician else "operator",
                step_id=STEP_MANUAL_CHECK,
                pass_next_step_id=_next_after_manual_check(
                    safety_context=safety_context,
                    torque_context=torque_context,
                    telemetry=telemetry,
                ),
                fail_next_step_id=(
                    STEP_ESCALATE_SAFETY
                    if safety_context
                    else _persistent_or_torque_target(
                        torque_context=torque_context,
                        telemetry=telemetry,
                    )
                ),
            )
        )

    if torque_context:
        steps.append(
            DiagnosticStep(
                label="Inspect the capping head and torque path",
                detail=(
                    "Check chuck wear, capper head movement, cap seating, and the configured "
                    "torque setpoint before clearing the alarm."
                ),
                priority="next",
                requires_technician=False,
                source="troubleshooting",
                evidence_title=manual_evidence.title if manual_evidence else None,
                page=manual_evidence.page if manual_evidence else None,
                expected_outcome="No obvious obstruction, wear, seating issue, or incorrect setpoint remains.",
                pass_follow_up="The capping head and torque path check is complete.",
                fail_follow_up="I checked the capping head and torque path and the issue is still present.",
                safety_level="operator",
                required_role="operator",
                step_id=STEP_TORQUE_PATH,
                pass_next_step_id=(
                    STEP_ESCALATE_PERSISTENT if telemetry and telemetry.has_active_alarm else None
                ),
                fail_next_step_id=STEP_ESCALATE_PERSISTENT,
            )
        )

    if telemetry and telemetry.temperature_c >= 45:
        steps.append(
            DiagnosticStep(
                label="Check for heat or friction contributors",
                detail=(
                    "Inspect lubrication, rotating components, and blocked motion before running "
                    "another production cycle."
                ),
                priority="next",
                requires_technician=False,
                source="telemetry",
                evidence_title=telemetry_evidence.title if telemetry_evidence else None,
                expected_outcome="The likely heat or friction contributor is found or ruled out.",
                pass_follow_up="The heat and friction check is complete.",
                fail_follow_up="I checked heat and friction contributors and the issue is still present.",
                safety_level="operator",
                required_role="operator",
                step_id=STEP_HEAT_FRICTION,
                pass_next_step_id=STEP_ESCALATE_PERSISTENT if telemetry.has_active_alarm else None,
                fail_next_step_id=STEP_ESCALATE_PERSISTENT,
            )
        )

    if safety_context:
        steps.append(
            DiagnosticStep(
                label="Escalate safety-circuit diagnostics",
                detail=(
                    "If the fault involves safety circuits, interlocks, phase sequence, guards, or "
                    "protected motion, stop operator troubleshooting and assign a qualified technician."
                ),
                priority="escalate",
                requires_technician=True,
                source="troubleshooting",
                evidence_title=manual_evidence.title if manual_evidence else None,
                page=manual_evidence.page if manual_evidence else None,
                expected_outcome="A qualified technician receives the fault, telemetry, and citation.",
                pass_follow_up="I escalated this safety-sensitive fault to a qualified technician.",
                fail_follow_up="I cannot escalate this safety-sensitive fault yet.",
                safety_level="safety-critical",
                required_role="technician",
                step_id=STEP_ESCALATE_SAFETY,
            )
        )
    elif telemetry and telemetry.has_active_alarm:
        steps.append(
            DiagnosticStep(
                label="Escalate if the alarm remains",
                detail=(
                    "If the alarm persists after operator checks, stop troubleshooting and send "
                    "the alarm code, telemetry snapshot, machine ID, and cited manual page to support."
                ),
                priority="escalate",
                requires_technician=True,
                source="troubleshooting",
                evidence_title=manual_evidence.title if manual_evidence else None,
                page=manual_evidence.page if manual_evidence else None,
                expected_outcome="Support receives a complete alarm, telemetry, machine, and citation packet.",
                pass_follow_up="I escalated the persistent alarm with telemetry and manual citation.",
                fail_follow_up="The alarm remains and I have not escalated it yet.",
                safety_level="technician",
                required_role="technician",
                step_id=STEP_ESCALATE_PERSISTENT,
            )
        )

    if not steps and manual_evidence:
        steps.append(
            DiagnosticStep(
                label=f"Review cited manual section: {manual_evidence.section or manual_evidence.title}",
                detail=_manual_detail(manual_evidence.excerpt),
                priority="next",
                requires_technician=False,
                source="manual",
                evidence_title=manual_evidence.title,
                page=manual_evidence.page,
                expected_outcome="The cited manual section has been reviewed against the machine state.",
                pass_follow_up=(
                    f"I reviewed the cited manual section: {manual_evidence.section or manual_evidence.title}."
                ),
                fail_follow_up=(
                    f"I reviewed {manual_evidence.section or manual_evidence.title!r} and still need help."
                ),
                safety_level="operator",
                required_role="operator",
                step_id=STEP_MANUAL_CHECK,
            )
        )

    return _dedupe_steps(steps)


def _is_torque_context(*, query: str, telemetry: TelemetrySnapshot | None) -> bool:
    return "torque" in query.lower() or bool(
        telemetry and "TORQUE" in telemetry.active_alarm.upper()
    )


def _next_after_record_alarm(
    *,
    safety_context: bool,
    manual_evidence: Evidence | None,
    torque_context: bool,
    telemetry: TelemetrySnapshot,
) -> str | None:
    if safety_context:
        return STEP_PRESERVE_SAFETY

    if manual_evidence:
        return STEP_MANUAL_CHECK

    if torque_context:
        return STEP_TORQUE_PATH

    if telemetry.temperature_c >= 45:
        return STEP_HEAT_FRICTION

    return STEP_ESCALATE_PERSISTENT


def _next_after_manual_check(
    *,
    safety_context: bool,
    torque_context: bool,
    telemetry: TelemetrySnapshot | None,
) -> str | None:
    if safety_context:
        return STEP_ESCALATE_SAFETY

    if torque_context:
        return STEP_TORQUE_PATH

    if telemetry and telemetry.temperature_c >= 45:
        return STEP_HEAT_FRICTION

    if telemetry and telemetry.has_active_alarm:
        return STEP_ESCALATE_PERSISTENT

    return None


def _persistent_or_torque_target(
    *,
    torque_context: bool,
    telemetry: TelemetrySnapshot | None,
) -> str | None:
    if torque_context:
        return STEP_TORQUE_PATH

    if telemetry and telemetry.has_active_alarm:
        return STEP_ESCALATE_PERSISTENT

    return None


def _manual_detail(excerpt: str, query: str = "") -> str:
    excerpt = focus_alarm_passage(excerpt, query)
    lines = [line.strip() for line in excerpt.splitlines() if line.strip()]
    cleaned = [
        line
        for line in lines
        if not line.lower().startswith("section:")
        and not (len(line) <= 90 and "operator manual" in line.lower())
    ]
    text = " ".join(cleaned or lines)
    normalized = text.upper()

    if "TORQUE ADJUSTMENT" in normalized or "ADJUSTMENTS OF THE CLOSURE HEAD" in normalized:
        return (
            "Use the cited torque-adjustment procedure: check the head/cap area, loosen the locking parts, "
            "adjust torque on the engraved scale/reference, tighten the locking parts again, and confirm the "
            "alarm does not return."
        )

    if "HEADS ADJUSTMENT" in normalized and "TORQUE" in normalized:
        return (
            "Use the cited head-adjustment procedure: loosen the locking nut and grub screw, rotate the head "
            "body to the required torque on the engraved scale, then tighten the grub screw and lock nut."
        )

    # Was `text[:600]`: the page number, the subsection index and the table rows
    # went to the operator verbatim, ending mid-word.
    return readable_passage(text)


def _manual_label(evidence: Evidence) -> str:
    title = evidence.section or evidence.title
    text = f"{title} {evidence.excerpt}".upper()

    if evidence.alarm_codes and any(term in text for term in ("ALARM", "ERROR", "FAULT")):
        return f"Alarm {', '.join(evidence.alarm_codes)}"

    if "TORQUE ADJUSTMENT" in text or "ADJUSTMENTS OF THE CLOSURE HEAD" in text:
        return "Torque adjustment"

    if title.lower().startswith("the table below"):
        return "Cited procedure"

    return title


def _manual_safety_level(evidence: Evidence, safety_context: bool) -> str:
    if safety_context:
        return "safety-critical"

    return evidence.safety_level or "operator"


def _manual_requires_technician(evidence: Evidence, safety_context: bool) -> bool:
    return safety_context or evidence.safety_level in {"technician", "safety-critical"}


def _manual_expected_outcome(evidence: Evidence, safety_context: bool) -> str:
    if safety_context or evidence.safety_level == "safety-critical":
        return "Only safe visual/operator checks are complete; protected electrical or guarded-area work is escalated."

    if evidence.safety_level == "technician":
        return "The cited check is reviewed and any technician-only work is escalated."

    label = _manual_label(evidence).lower()
    if "torque" in label:
        return "The cited torque check is complete and the alarm state is known."

    return "The cited manual check is complete and the result is known."


def _is_safety_context(
    *,
    query: str,
    telemetry: TelemetrySnapshot | None,
    evidence: Evidence | None,
) -> bool:
    text = query
    if telemetry:
        text += f" {telemetry.active_alarm}"
    if evidence:
        text += f" {evidence.title} {evidence.section or ''} {evidence.excerpt}"
        if evidence.safety_level == "safety-critical":
            return True
        if evidence.chunk_kind == "safety":
            return True
        if set(evidence.topics).intersection({"emergency", "interlock", "safety"}):
            return True

    normalized = text.lower()
    return any(
        phrase in normalized
        for phrase in (
            "safety circuit",
            "interlock",
            "emergency",
            "guard",
            "phase sequence",
            "door lock",
            "no voltage",
            "bypass",
        )
    )


def _dedupe_steps(steps: list[DiagnosticStep]) -> list[DiagnosticStep]:
    seen: set[str] = set()
    deduped: list[DiagnosticStep] = []
    for step in steps:
        key = step.label.lower()
        if key in seen:
            continue

        seen.add(key)
        deduped.append(step)

    return deduped
