from pathlib import Path
from types import SimpleNamespace

import pytest

from arol_ai.access import AccessContext, Domain
from arol_ai.agents.maintenance import assess_maintenance, read_schedule
from arol_ai.graph.nodes import business_agent_node, business_data_requested
from arol_ai.graph.state import GraphState
from arol_ai.tools.registry import build_tool_registry

MANUAL = Path(__file__).resolve().parents[2] / "requirements/manuals/A4344_manual_EN.pdf"


def test_intervals_come_from_task_sections_not_contents():
    schedule = read_schedule(str(MANUAL), MANUAL.stat().st_mtime_ns)
    first = next(item for item in schedule if item["hours"] == 40)
    assert first["page"] == 94
    assert "Pneumatic system check" in first["task"]
    assert any(item["hours"] == 500 for item in schedule)


def test_due_calculation_excludes_future_and_does_not_reset_on_ticket(monkeypatch):
    monkeypatch.setenv("PLATFORM_TODAY", "2026-08-05")
    rows = [{"timestamp": "2026-08-04T00:00:00Z", "uptimePercentage": 50} for _ in range(100)]
    rows += [{"timestamp": "2026-08-06T00:00:00Z", "uptimePercentage": 100}]
    tickets = [SimpleNamespace(ticket_type="Scheduled maintenance", created_date="2026-08-04")]
    bullets, evidence = assess_maintenance(MANUAL, "/manuals/A4344_manual_EN.pdf", rows, tickets)
    text = "\n".join(bullets)
    assert "50.0 h" in text
    assert "50.0 >= 40 h" in text
    assert "450.0 productive hours remain" in text
    assert "not proof" in text
    assert "calendar due date requires" in text
    assert evidence[0].page == 94


@pytest.mark.parametrize(
    "visibility,denied", [("technician", False), ("full", False), ("commercial", True)]
)
def test_maintenance_is_operational_even_when_served_by_business(visibility, denied):
    state = GraphState(
        session_id="test",
        machine_id="MCH-0001",
        user_message="What maintenance activities were recently performed?",
        access=AccessContext(company_id="CMP-001", visibility=visibility),
    )
    state.agent_trace = ["supervisor", "business-agent"]
    business_agent_node(state, None, [], [], {}, build_tool_registry())
    assert bool(state.access_denials) == denied
    if denied:
        assert state.access_denials[0].domain == Domain.OPERATIONAL
    else:
        assert any(call.name == "business.list_service_history" for call in state.tool_calls)


@pytest.mark.parametrize(
    "query",
    [
        "What maintenance activities were recently performed?",
        "When is the next service?",
        "What service due checks should I perform?",
    ],
)
def test_recent_maintenance_does_not_request_commercial_standing(query):
    requested = business_data_requested(query)
    assert requested["service_history"]
    assert not requested["entitlement"]
