from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

ToolStatus = Literal["ok", "empty", "error"]


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    agent: str
    status: ToolStatus
    input_summary: str
    output_summary: str
    id: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        agent: str,
        status: ToolStatus,
        input_summary: str,
        output_summary: str,
    ) -> "ToolCallRecord":
        return cls(
            id=str(uuid4()),
            name=name,
            agent=agent,
            status=status,
            input_summary=input_summary,
            output_summary=output_summary,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "agent": self.agent,
            "status": self.status,
            "inputSummary": self.input_summary,
            "outputSummary": self.output_summary,
        }
