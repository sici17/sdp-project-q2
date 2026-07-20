from datetime import datetime, timezone
from uuid import uuid4

from arol_ai.data.repository import MachineContextRepository
from arol_ai.domain.models import MachineContext
from arol_ai.graph.orchestrator import Orchestrator


class EscalationDraftBuilder:
    def __init__(self, repository: MachineContextRepository) -> None:
        self.repository = repository
        self.orchestrator = Orchestrator(repository=repository)

    def build(self, request: dict) -> dict:
        context = self.repository.get_machine_context(request["machineId"])
        if context.machine is None:
            raise ValueError("Machine not found.")

        assistant_response = self.orchestrator.complete_chat(request)

        return {
            "id": str(uuid4()),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "machineId": request["machineId"],
            "sessionId": request["sessionId"],
            "severity": _severity(context),
            "title": _title(context),
            "summary": assistant_response["message"]["content"],
            "machine": context.machine.to_dict(),
            "telemetry": context.telemetry.to_dict() if context.telemetry else None,
            "contract": context.contract.to_dict() if context.contract else None,
            "agentTrace": assistant_response["agentTrace"],
            "toolCalls": assistant_response.get("toolCalls", []),
            "evidence": assistant_response["evidence"],
            "recommendedActions": assistant_response.get("recommendedActions", []),
        }


def _severity(context: MachineContext) -> str:
    telemetry = context.telemetry

    if telemetry and telemetry.health == "critical":
        return "critical"

    if telemetry and telemetry.has_active_alarm:
        return "warning"

    if context.machine and context.machine.status in {"critical", "warning", "offline"}:
        return context.machine.status

    return "normal"


def _title(context: MachineContext) -> str:
    machine = context.machine
    telemetry = context.telemetry

    if machine and telemetry and telemetry.has_active_alarm:
        return f"{machine.model} active alarm {telemetry.active_alarm}"

    if machine:
        return f"{machine.model} support escalation"

    return "Machine support escalation"
