"""The dataset's access model, enforced inside the AI service.

The gateway already checks every REST route, but the agents reach data through
their own tools, so the same two checks have to hold here as well. Enforcing it
in the tool layer rather than in a prompt matters: a prompt instruction is a
request, and a tool that returns rows regardless will hand them over whatever
the model was told.

Two independent checks, both of which must pass:

1. ``companyId`` is the tenant boundary and is never crossed.
2. ``visibility`` narrows which data domains the user reaches inside their own
   company.

A denied request produces a typed refusal that the answer layer turns into an
explicit decline. It is never answered from another company's data, and never
returned as an empty result as though no data existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Domain(str, Enum):
    """The three domains the dataset splits into."""

    #: Machines, MachineModels and the machine manuals.
    COMMON = "common"
    #: TelemetrySnapshots, Alarms, MaintenanceTickets.
    OPERATIONAL = "operational"
    #: Quotes, QuoteRevisions, QuoteLines, Orders, OrderLines.
    COMMERCIAL = "commercial"


VISIBILITY_DOMAINS: dict[str, frozenset[Domain]] = {
    "full": frozenset({Domain.COMMON, Domain.OPERATIONAL, Domain.COMMERCIAL}),
    "technician": frozenset({Domain.COMMON, Domain.OPERATIONAL}),
    "commercial": frozenset({Domain.COMMON, Domain.COMMERCIAL}),
}

_DOMAIN_LABEL = {
    Domain.OPERATIONAL: "telemetry, alarms and maintenance history",
    Domain.COMMERCIAL: "quotes, orders and pricing",
    Domain.COMMON: "machine and manual information",
}

#: Who can see the domain instead, so a decline can point somewhere useful.
_DOMAIN_HOLDER = {
    Domain.OPERATIONAL: "a colleague with technician or full access",
    Domain.COMMERCIAL: "a colleague with commercial or full access",
    Domain.COMMON: "AROL support",
}


@dataclass(frozen=True)
class AccessContext:
    """Who is asking, and what they are allowed to reach."""

    user_id: str | None = None
    company_id: str | None = None
    visibility: str | None = None
    #: AROL staff serve every customer, so they are not bound to one tenant.
    is_staff: bool = False
    #: True when the gateway runs with authentication disabled for local work.
    auth_disabled: bool = False

    @property
    def unrestricted(self) -> bool:
        return self.auth_disabled or self.is_staff

    @property
    def domains(self) -> frozenset[Domain]:
        if self.unrestricted:
            return frozenset(Domain)
        return VISIBILITY_DOMAINS.get(self.visibility or "", frozenset())

    def can_see(self, domain: Domain) -> bool:
        return domain in self.domains

    def owns(self, company_id: str | None) -> bool:
        if self.unrestricted:
            return True
        if not self.company_id or not company_id:
            # Fail closed: an unknown tenant on either side is not a match.
            return False
        return self.company_id == company_id


#: Used when no identity reached the service at all. It can see nothing, which
#: is the only safe reading of "we do not know who this is".
ANONYMOUS = AccessContext()


@dataclass(frozen=True)
class AccessDenied:
    """A refusal a tool returns instead of data.

    Carries enough for the answer layer to say what was refused and why,
    without the caller having to guess from an empty list.
    """

    domain: Domain
    reason: str
    message: str
    visibility: str | None = None
    machine_id: str | None = None

    @property
    def status(self) -> str:
        return "access_denied"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "domain": self.domain.value,
            "reason": self.reason,
            "message": self.message,
            "visibility": self.visibility,
            "machineId": self.machine_id,
        }


def check_domain(
    access: AccessContext, domain: Domain, *, machine_id: str | None = None
) -> AccessDenied | None:
    """Return a refusal if this identity may not read ``domain``, else None."""
    if access.can_see(domain):
        return None

    if not access.visibility:
        return AccessDenied(
            domain=domain,
            reason="visibility_unknown",
            message=(
                "I could not confirm your access level, so I cannot show "
                f"{_DOMAIN_LABEL[domain]}. Please sign in again."
            ),
            machine_id=machine_id,
        )

    return AccessDenied(
        domain=domain,
        reason="visibility_denied",
        message=(
            f"Your {access.visibility} access does not include {_DOMAIN_LABEL[domain]} "
            f"for this machine. {_DOMAIN_HOLDER[domain].capitalize()} can see it."
        ),
        visibility=access.visibility,
        machine_id=machine_id,
    )


def check_tenancy(
    access: AccessContext, company_id: str | None, *, machine_id: str | None = None
) -> AccessDenied | None:
    """Return a refusal if the machine is outside this identity's company."""
    if access.owns(company_id):
        return None

    return AccessDenied(
        domain=Domain.COMMON,
        reason="machine_not_in_company",
        message=(
            "That machine belongs to another company's fleet, so I cannot show anything about it."
        ),
        machine_id=machine_id,
    )
