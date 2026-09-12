"""What happens when the model that rewrites the answer fails.

The composer has already built a grounded draft from retrieved evidence by the
time a provider is called; the model's job is only to write it well. Losing the
whole turn because that rewrite timed out would throw away an answer we already
have — and on a small local model running on CPU, a timeout is an ordinary
event, not an exceptional one.
"""

import pytest

from arol_ai.access import AccessContext
from arol_ai.domain.models import Machine, MachineContext, Manual
from arol_ai.graph.orchestrator import Orchestrator
from arol_ai.llm.providers import LLMProviderResponseError
from arol_ai.tools.business_tools import ServiceEntitlementTool
from arol_ai.tools.doc_tools import ManualSearchTool
from arol_ai.tools.registry import ToolRegistry
from arol_ai.tools.telemetry_tools import TelemetrySnapshotTool
from arol_ai.tools.troubleshooting_tools import TroubleshootingTool

ACCESS = AccessContext(user_id="USR-001", company_id="CMP-001", visibility="full")


class _Repository:
    def get_machine_context(self, machine_id: str) -> MachineContext:
        return MachineContext(
            machine=Machine(
                id=machine_id,
                company_id="CMP-001",
                serial_number="15610",
                model="TS-EURO-PK-TWIN-CHUTE-D",
                plant="Novara Plant 1 - Bottling Line 3",
                status="ok",
            ),
            manual=Manual(
                machine_id=machine_id,
                title="manual",
                version=None,
                language="en",
                url="/manuals/15610_manual_EN.pdf",
            ),
            telemetry=None,
            contract=None,
        )


class _FailingProvider:
    name = "ollama"

    def synthesize(self, context: dict) -> str:
        raise LLMProviderResponseError("ollama network request failed.")

    def synthesize_stream(self, context: dict):
        raise LLMProviderResponseError("ollama network request failed.")
        yield ""  # pragma: no cover - generator marker


class _PartialProvider(_FailingProvider):
    def synthesize_stream(self, context: dict):
        yield "Checked: "
        raise LLMProviderResponseError("ollama streaming failed.")


def _orchestrator(provider) -> Orchestrator:
    return Orchestrator(
        repository=_Repository(),
        tools=ToolRegistry(
            manual_search=ManualSearchTool(rag_enabled=False),
            telemetry_snapshot=TelemetrySnapshotTool(),
            troubleshooting=TroubleshootingTool(),
            service_entitlement=ServiceEntitlementTool(),
        ),
        llm_provider=provider,
    )


def _request(session_id: str) -> dict:
    return {
        "sessionId": session_id,
        "machineId": "MCH-0001",
        "message": "What does the manual say about the caps chute?",
        "messages": [],
        "access": ACCESS,
    }


def test_a_failed_rewrite_answers_from_the_grounded_draft() -> None:
    response = _orchestrator(_FailingProvider()).complete_chat(_request("fallback-1"))

    assert response["message"]["content"], "the turn produced no answer at all"
    assert "Checked:" in response["message"]["content"]
    # The failure is recorded rather than hidden: a run where every answer came
    # from the draft should be visible in the trace.
    failures = [item for item in response["toolCalls"] if item["name"] == "llm.synthesize"]
    assert failures and failures[0]["status"] == "error"
    assert "grounded draft" in failures[0]["outputSummary"]


def test_a_stream_that_fails_before_any_token_still_delivers_the_answer() -> None:
    events = list(_orchestrator(_FailingProvider()).stream_chat_events(_request("fallback-2")))
    tokens = "".join(payload["delta"] for name, payload in events if name == "token")

    assert "Checked:" in tokens
    done = next(payload for name, payload in events if name == "done")
    assert done["message"]["content"] == tokens


def test_a_stream_that_fails_midway_keeps_what_was_sent_and_finishes_the_draft() -> None:
    """Tokens already delivered cannot be retracted, so the rest is appended."""
    events = list(_orchestrator(_PartialProvider()).stream_chat_events(_request("fallback-3")))
    tokens = "".join(payload["delta"] for name, payload in events if name == "token")

    assert tokens.startswith("Checked: ")
    assert tokens.count("Checked:") == 2, "the draft should follow the partial output"


def test_a_working_provider_is_still_used() -> None:
    class _Provider:
        name = "test"

        def synthesize(self, context: dict) -> str:
            return "rewritten answer"

    response = _orchestrator(_Provider()).complete_chat(_request("fallback-4"))

    assert response["message"]["content"] == "rewritten answer"
    assert not [item for item in response["toolCalls"] if item["name"] == "llm.synthesize"]


@pytest.mark.parametrize("provider", [_FailingProvider(), _PartialProvider()])
def test_the_answer_is_never_empty_however_synthesis_fails(provider) -> None:
    events = list(_orchestrator(provider).stream_chat_events(_request("fallback-5")))
    done = next(payload for name, payload in events if name == "done")

    assert done["message"]["content"].strip()
