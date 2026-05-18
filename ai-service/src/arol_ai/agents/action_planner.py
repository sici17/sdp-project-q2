from arol_ai.alarm_codes import contains_alarm_code, extract_alarm_code
from arol_ai.domain.models import ServiceContract, TelemetrySnapshot
from arol_ai.graph.evidence_selection import select_manual_evidence
from arol_ai.graph.state import ActionItem, Evidence, GraphState

SAFETY_QUERY_TERMS = {"door", "emergency", "guard", "interlock", "safety", "stop", "voltage"}


def plan_actions(
    state: GraphState,
    telemetry: TelemetrySnapshot | None,
    contract: ServiceContract | None,
) -> list[ActionItem]:
    if state.safety_blocked:
        return [
            ActionItem(
                label="Stop the machine and keep all guards, interlocks, and emergency stops active.",
                priority="immediate",
                requires_technician=False,
                source="safety",
            ),
            ActionItem(
                label="Escalate to a qualified AROL technician before restarting.",
                priority="escalate",
                requires_technician=True,
                source="safety",
            ),
        ]

    if any(intent in state.intents for intent in ("conversation", "clarification")):
        return []

    actions: list[ActionItem] = []
    telemetry_route_active = (
        "telemetry-agent" in state.agent_trace or "troubleshooting-agent" in state.agent_trace
    )
    requested_alarm_code = extract_alarm_code(state.user_message)
    current_alarm_is_relevant = not requested_alarm_code or bool(
        telemetry and contains_alarm_code(telemetry.active_alarm, requested_alarm_code)
    )

    if (
        telemetry_route_active
        and telemetry
        and telemetry.has_active_alarm
        and current_alarm_is_relevant
    ):
        actions.append(
            ActionItem(
                label=f"Record active alarm {telemetry.active_alarm} with the latest telemetry snapshot.",
                priority="immediate",
                requires_technician=False,
                source="telemetry",
            )
        )

    # The same passage the citation names. Ranking it separately here is
    # what put a different manual section in the recommended actions.
    manual_evidence = select_manual_evidence(state.evidence, state.operator_context)
    if manual_evidence:
        manual_requires_technician = _manual_requires_technician(manual_evidence)
        actions.append(
            ActionItem(
                label=_manual_action_text(manual_evidence),
                priority="escalate" if manual_requires_technician else "next",
                requires_technician=manual_requires_technician,
                source="manual",
            )
        )

    if (
        telemetry_route_active
        and telemetry
        and telemetry.has_active_alarm
        and current_alarm_is_relevant
    ):
        actions.append(
            ActionItem(
                label="Escalate if the alarm persists after the operator checks.",
                priority="escalate",
                requires_technician=True,
                source="telemetry",
            )
        )

    # Open maintenance work is the only support-routing signal the dataset
    # actually carries; there is no SLA in it to route by.
    if contract and contract.open_ticket_count and "business-agent" in state.agent_trace:
        actions.append(
            ActionItem(
                label=(
                    f"Check the {contract.open_ticket_count} open maintenance "
                    "ticket(s) before opening a new one."
                ),
                priority="next",
                requires_technician=False,
                source="business",
            )
        )

    if not actions:
        actions.append(
            ActionItem(
                label="Monitor the machine state and capture alarm details if conditions change.",
                priority="next",
                requires_technician=False,
                source="supervisor",
            )
        )

    return actions


def _manual_action_text(manual_evidence: Evidence) -> str:
    label = _manual_action_label(manual_evidence)
    if _manual_requires_technician(manual_evidence):
        return (
            "Review the cited safety-sensitive manual section with a qualified technician: "
            f"{label}."
        )

    return f"Follow the cited manual procedure: {label}."


def _manual_requires_technician(manual_evidence: Evidence) -> bool:
    return manual_evidence.chunk_kind == "safety" or manual_evidence.safety_level in {
        "technician",
        "safety-critical",
    }


def _manual_action_label(manual_evidence: Evidence) -> str:
    title = manual_evidence.section or manual_evidence.title
    text = f"{title} {manual_evidence.excerpt}".upper()

    if "TORQUE ADJUSTMENT" in text or "ADJUSTMENTS OF THE CLOSURE HEAD" in text:
        return "torque adjustment"

    if title.lower().startswith("the table below"):
        return "the cited procedure"

    return title.rstrip(".")
