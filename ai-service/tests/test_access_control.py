"""The dataset's access model, enforced inside the AI service.

The rule under test, from the dataset brief: two independent checks must both
pass before any data is returned, and a request that falls outside a user's
scope must be declined explicitly. It must never be answered from another
company's data, and never returned as an empty result as though no data existed.

That last clause is what most of these tests are really about. A denial that
looks like an empty result is the failure mode that matters, because an operator
cannot tell it apart from a healthy machine with nothing to report.
"""

import pytest

from arol_ai.access import (
    ANONYMOUS,
    AccessContext,
    Domain,
    check_domain,
    check_tenancy,
)
from arol_ai.graph.state import Evidence, GraphState

FULL = AccessContext(user_id="USR-001", company_id="CMP-001", visibility="full")
TECHNICIAN = AccessContext(user_id="USR-002", company_id="CMP-001", visibility="technician")
COMMERCIAL = AccessContext(user_id="USR-004", company_id="CMP-001", visibility="commercial")
STAFF = AccessContext(user_id="arol-support", is_staff=True)


# ---------------------------------------------------------------------------
# The visibility matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("access", "domain", "allowed"),
    [
        # Machine identity and documentation is visible to every user of the
        # owning company, whatever their visibility.
        (FULL, Domain.COMMON, True),
        (TECHNICIAN, Domain.COMMON, True),
        (COMMERCIAL, Domain.COMMON, True),
        # Only a technician (or full) can ask how a machine is running.
        (FULL, Domain.OPERATIONAL, True),
        (TECHNICIAN, Domain.OPERATIONAL, True),
        (COMMERCIAL, Domain.OPERATIONAL, False),
        # Only a commercial (or full) user can ask what it cost.
        (FULL, Domain.COMMERCIAL, True),
        (TECHNICIAN, Domain.COMMERCIAL, False),
        (COMMERCIAL, Domain.COMMERCIAL, True),
    ],
)
def test_visibility_matrix(access: AccessContext, domain: Domain, allowed: bool) -> None:
    assert access.can_see(domain) is allowed
    assert (check_domain(access, domain) is None) is allowed


def test_denial_names_the_domain_and_who_can_help() -> None:
    denial = check_domain(TECHNICIAN, Domain.COMMERCIAL, machine_id="MCH-0001")

    assert denial is not None
    assert denial.reason == "visibility_denied"
    assert denial.domain is Domain.COMMERCIAL
    assert "technician" in denial.message
    assert "quotes" in denial.message
    # A refusal should point somewhere, not just close the door.
    assert "commercial or full access" in denial.message


def test_unknown_visibility_is_denied_rather_than_assumed() -> None:
    unknown = AccessContext(user_id="USR-999", company_id="CMP-001", visibility=None)

    for domain in Domain:
        denial = check_domain(unknown, domain)
        assert denial is not None
        assert denial.reason == "visibility_unknown"


def test_anonymous_identity_can_see_nothing() -> None:
    assert ANONYMOUS.domains == frozenset()
    for domain in Domain:
        assert check_domain(ANONYMOUS, domain) is not None


def test_staff_serve_every_customer() -> None:
    for domain in Domain:
        assert check_domain(STAFF, domain) is None
    assert check_tenancy(STAFF, "CMP-004") is None


# ---------------------------------------------------------------------------
# The tenant boundary
# ---------------------------------------------------------------------------


def test_tenant_boundary_is_never_crossed_at_any_visibility() -> None:
    for access in (FULL, TECHNICIAN, COMMERCIAL):
        denial = check_tenancy(access, "CMP-004", machine_id="MCH-0006")
        assert denial is not None, f"{access.visibility} crossed the tenant boundary"
        assert denial.reason == "machine_not_in_company"
        assert "another company" in denial.message


def test_own_company_passes_tenancy() -> None:
    assert check_tenancy(TECHNICIAN, "CMP-001", machine_id="MCH-0001") is None


def test_missing_company_on_either_side_fails_closed() -> None:
    no_company = AccessContext(user_id="USR-001", company_id=None, visibility="full")

    assert check_tenancy(no_company, "CMP-001") is not None
    assert check_tenancy(FULL, None) is not None


# ---------------------------------------------------------------------------
# Refusals reach the answer, rather than looking like absence
# ---------------------------------------------------------------------------


def _state(access: AccessContext) -> GraphState:
    return GraphState(
        session_id="s",
        machine_id="MCH-0001",
        user_message="what did this machine cost?",
        access=access,
    )


def test_denials_are_recorded_on_the_graph_state() -> None:
    state = _state(TECHNICIAN)
    denial = check_domain(state.access, Domain.COMMERCIAL, machine_id=state.machine_id)
    assert denial is not None
    state.deny(denial)

    assert [item.domain for item in state.access_denials] == [Domain.COMMERCIAL]
    payload = state.to_response()
    assert payload["accessDenials"][0]["reason"] == "visibility_denied"
    assert payload["accessDenials"][0]["domain"] == "commercial"


def test_repeated_denials_are_not_duplicated() -> None:
    state = _state(TECHNICIAN)
    for _ in range(3):
        state.deny(check_domain(state.access, Domain.COMMERCIAL))

    assert len(state.access_denials) == 1


def test_denied_turn_answers_with_an_explicit_refusal() -> None:
    from arol_ai.graph.nodes import answer_node

    state = _state(TECHNICIAN)
    state.agent_trace = ["supervisor", "business-agent"]
    state.deny(check_domain(state.access, Domain.COMMERCIAL, machine_id="MCH-0001"))

    answer_node(state, machine_label="MCH-0001", telemetry=None, contract=None)

    # The refusal must be stated, and must not read as "there is nothing here".
    assert "cannot answer" in state.answer.lower()
    assert "technician" in state.answer
    assert "no orders were found" not in state.answer.lower()
    assert "not found" not in state.answer.lower()


def test_partial_denial_still_states_what_was_withheld() -> None:
    from arol_ai.graph.nodes import answer_node
    from arol_ai.graph.state import Evidence

    state = _state(TECHNICIAN)
    state.agent_trace = ["supervisor", "doc-agent", "business-agent"]
    state.evidence.append(
        Evidence(
            source="manual",
            title="Torque adjustment",
            excerpt="Torque adjustment procedure.",
            page=12,
            confidence=0.8,
        )
    )
    state.deny(check_domain(state.access, Domain.COMMERCIAL, machine_id="MCH-0001"))

    answer_node(state, machine_label="MCH-0001", telemetry=None, contract=None)

    # The allowed half is answered, and the withheld half is named rather than
    # quietly omitted.
    assert "Not available to you" in state.answer
    assert "quotes" in state.answer


# ---------------------------------------------------------------------------
# The tenant boundary inside the graph
#
# The gateway checks it, and so does the AI service's HTTP layer for machine
# context. Neither covers a chat turn reaching the orchestrator by any other
# route, so the graph holds the boundary itself.
# ---------------------------------------------------------------------------


def _other_company_orchestrator():
    from arol_ai.domain.models import Machine, MachineContext, Manual
    from arol_ai.graph.orchestrator import Orchestrator
    from arol_ai.tools.business_tools import ServiceEntitlementTool
    from arol_ai.tools.doc_tools import ManualSearchTool
    from arol_ai.tools.registry import ToolRegistry
    from arol_ai.tools.telemetry_tools import TelemetrySnapshotTool
    from arol_ai.tools.troubleshooting_tools import TroubleshootingTool

    class _Repository:
        def get_machine_context(self, machine_id: str) -> MachineContext:
            return MachineContext(
                machine=Machine(
                    id=machine_id,
                    company_id="CMP-004",
                    serial_number="A2064",
                    model="EAGLE-PK-TURRET",
                    plant="Glasgow Plant 1 - Line 2",
                    status="ok",
                ),
                manual=Manual(
                    machine_id=machine_id,
                    title="EAGLE PK manual",
                    version=None,
                    language="en",
                    url="/manuals/A2064_manual_EN.pdf",
                ),
                telemetry=None,
                contract=None,
            )

    return Orchestrator(
        repository=_Repository(),
        tools=ToolRegistry(
            manual_search=ManualSearchTool(rag_enabled=False),
            telemetry_snapshot=TelemetrySnapshotTool(),
            troubleshooting=TroubleshootingTool(),
            service_entitlement=ServiceEntitlementTool(),
        ),
    )


def test_a_machine_in_another_company_is_refused_before_any_agent_runs() -> None:
    response = _other_company_orchestrator().complete_chat(
        {
            "sessionId": "tenancy-1",
            "machineId": "MCH-0006",
            "message": "What does the manual say about the closure carousel?",
            "messages": [],
            "access": FULL,  # full visibility, but of a different company
        }
    )

    # Full visibility inside your own company buys nothing outside it, and the
    # refusal has to happen before retrieval rather than after.
    assert response["agentTrace"] == ["supervisor"]
    assert response["evidence"] == []
    assert response["toolCalls"] == []
    assert response["accessDenials"][0]["reason"] == "machine_not_in_company"
    assert "another company" in response["message"]["content"]


def test_the_refusal_does_not_describe_the_other_company_s_machine() -> None:
    response = _other_company_orchestrator().complete_chat(
        {
            "sessionId": "tenancy-2",
            "machineId": "MCH-0006",
            "message": "What is this machine?",
            "messages": [],
            "access": FULL,
        }
    )

    # Naming the model back to the asker would leak from the very records the
    # reply is refusing to read.
    content = response["message"]["content"]
    assert "EAGLE-PK-TURRET" not in content
    assert "Glasgow" not in content


def test_a_partial_refusal_is_not_rewritten_by_the_model() -> None:
    """The denial has to reach the operator as the composer wrote it.

    Rewritten by a small model it came back as step 2 of the "Recommended
    diagnostic path" - a denial presented as something to go and do - and then
    repeated as a trailing paragraph.
    """
    from arol_ai.graph.nodes import _synthesize_with_provider

    class Rewriter:
        name = "rewriter"

        def synthesize(self, context: dict) -> str:
            return "A rewritten answer that merged the sections."

        def synthesize_stream(self, context: dict):
            yield "A rewritten answer that merged the sections."

    state = GraphState(
        session_id="denied",
        machine_id="euro-vp-2019-01",
        user_message="Show me the telemetry",
        access=AccessContext(user_id="USR-004", company_id="CMP-001", visibility="commercial"),
    )
    denial = check_domain(
        AccessContext(user_id="USR-004", company_id="CMP-001", visibility="commercial"),
        Domain.OPERATIONAL,
    )
    assert denial is not None
    state.deny(denial)
    state.evidence = [Evidence(source="manual", title="T", excerpt="An excerpt.", page=1)]

    answer = _synthesize_with_provider(
        state=state,
        machine_label="TS-EURO (15610)",
        telemetry=None,
        contract=None,
        orders=[],
        service_history=[],
        quotes=[],
        llm_provider=Rewriter(),
        draft=(
            "Manual summary\n- An excerpt.\n"
            "Not available to you\n- Your commercial access does not include telemetry."
        ),
        structured_answer={},
    )

    assert "Not available to you" in answer
    assert "rewritten answer" not in answer
