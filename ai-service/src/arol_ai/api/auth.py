from dataclasses import dataclass
from hmac import compare_digest

from fastapi import HTTPException, Request

from arol_ai.access import ANONYMOUS, AccessContext, check_tenancy
from arol_ai.config import Settings


@dataclass(frozen=True)
class InternalAuthContext:
    subject: str
    roles: tuple[str, ...]
    machine_ids: frozenset[str]
    request_id: str | None
    access: AccessContext = ANONYMOUS

    @property
    def has_machine_restriction(self) -> bool:
        return len(self.machine_ids) > 0


def require_internal_auth(request: Request, settings: Settings) -> InternalAuthContext:
    configured_secrets = configured_internal_secrets(settings)
    presented_secret = request.headers.get("x-arol-internal-secret", "")

    if not configured_secrets:
        raise HTTPException(
            status_code=503,
            detail="AI service internal authentication is not configured.",
        )

    if not any(
        compare_digest(configured_secret, presented_secret)
        for configured_secret in configured_secrets
    ):
        raise HTTPException(status_code=401, detail="AI service internal auth failed.")

    return InternalAuthContext(
        subject=request.headers.get("x-arol-subject") or "anonymous",
        roles=tuple(_csv(request.headers.get("x-arol-roles"))),
        machine_ids=frozenset(_csv(request.headers.get("x-arol-machine-ids"))),
        request_id=request.headers.get("x-request-id"),
        access=_access_context(request),
    )


def _access_context(request: Request) -> AccessContext:
    """Read the access model from the gateway's trusted internal headers.

    These arrive only behind the shared secret verified above, so they are the
    gateway's assertion about the signed-in user rather than anything a browser
    can set.
    """
    return AccessContext(
        user_id=request.headers.get("x-arol-user-id") or None,
        company_id=request.headers.get("x-arol-company-id") or None,
        visibility=request.headers.get("x-arol-visibility") or None,
        is_staff=(request.headers.get("x-arol-is-staff") or "").lower() == "true",
        auth_disabled=(request.headers.get("x-arol-auth-mode") or "").lower() == "off",
    )


def configured_internal_secrets(settings: Settings) -> tuple[str, ...]:
    return settings.ai_service_shared_secrets or (
        (settings.ai_service_shared_secret,) if settings.ai_service_shared_secret else ()
    )


def enforce_machine_access(auth_context: InternalAuthContext, machine_id: str) -> None:
    if auth_context.has_machine_restriction and machine_id not in auth_context.machine_ids:
        raise HTTPException(
            status_code=403, detail="Machine access is not allowed for this identity."
        )


def enforce_tenancy(
    auth_context: InternalAuthContext, machine_company_id: str | None, machine_id: str
) -> None:
    """Reject a machine outside the caller's company.

    Declined explicitly rather than as a 404, because pretending the machine
    does not exist would be its own untruth: the operator scanned a real one.
    """
    denial = check_tenancy(auth_context.access, machine_company_id, machine_id=machine_id)
    if denial is not None:
        raise HTTPException(status_code=403, detail=denial.message)


def enforce_any_role(auth_context: InternalAuthContext, allowed_roles: set[str]) -> None:
    if not allowed_roles.intersection(auth_context.roles):
        raise HTTPException(
            status_code=403, detail="This identity does not have the required role."
        )


def _csv(value: str | None) -> list[str]:
    if not value:
        return []

    return [item.strip() for item in value.split(",") if item.strip()]
