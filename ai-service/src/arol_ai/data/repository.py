from typing import Protocol

from arol_ai.domain.models import MachineContext


class MachineContextRepository(Protocol):
    def get_machine_context(self, machine_id: str) -> MachineContext:
        """Return all context needed by the orchestrator for one machine."""
        ...

    def list_machine_ids(self) -> list[str]:
        """Return machine IDs known to the repository."""
        ...
