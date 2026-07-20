import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import RLock
from typing import Protocol
from uuid import uuid4

DEFAULT_MAX_SESSION_MESSAGES = 100
DEFAULT_MAX_SESSION_BYTES = 512 * 1024
DEFAULT_IDEMPOTENCY_PENDING_LEASE_SECONDS = 120


@dataclass(frozen=True)
class SessionMetadata:
    session_id: str
    machine_id: str
    owner_subject: str
    created_at: str
    expires_at: str
    message_count: int
    diagnostic_state: dict | None = None

    def to_dict(self) -> dict:
        payload = {
            "sessionId": self.session_id,
            "machineId": self.machine_id,
            "ownerSubject": self.owner_subject,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "messageCount": self.message_count,
        }
        if self.diagnostic_state is not None:
            payload["diagnosticState"] = self.diagnostic_state
        return payload


@dataclass(frozen=True)
class ChatSession:
    metadata: SessionMetadata
    messages: list[dict]

    def to_dict(self) -> dict:
        return {
            **self.metadata.to_dict(),
            "messages": [
                {key: value for key, value in message.items() if not key.startswith("_")}
                for message in self.messages
            ],
        }


class SessionStore(Protocol):
    persistence_type: str

    def create_session(self, *, machine_id: str, owner_subject: str) -> SessionMetadata: ...

    def create_session_idempotent(
        self,
        *,
        machine_id: str,
        owner_subject: str,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
    ) -> SessionMetadata: ...

    def get_session(
        self,
        session_id: str,
        *,
        owner_subject: str,
        machine_id: str | None = None,
        include_messages: bool = False,
    ) -> ChatSession: ...

    def append_message(
        self,
        session_id: str,
        *,
        owner_subject: str,
        machine_id: str,
        message: dict,
    ) -> SessionMetadata: ...

    def update_diagnostic_state(
        self,
        session_id: str,
        *,
        owner_subject: str,
        machine_id: str,
        diagnostic_state: dict | None,
    ) -> SessionMetadata: ...

    def begin_idempotent_operation(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
    ) -> dict | None:
        """Claim an operation or return its completed response for a safe replay."""
        ...

    def complete_idempotent_operation(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
        response: dict,
    ) -> None: ...

    def abort_idempotent_operation(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
    ) -> None: ...


class SessionError(Exception):
    status_code = 500


class SessionNotFound(SessionError):
    status_code = 404


class SessionForbidden(SessionError):
    status_code = 403


class IdempotencyConflict(SessionError):
    status_code = 409


class IdempotencyInProgress(SessionError):
    status_code = 409


class InMemorySessionStore:
    persistence_type = "in-memory"

    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_messages: int = DEFAULT_MAX_SESSION_MESSAGES,
        max_bytes: int = DEFAULT_MAX_SESSION_BYTES,
        pending_lease_seconds: int = DEFAULT_IDEMPOTENCY_PENDING_LEASE_SECONDS,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self.pending_lease_seconds = min(ttl_seconds, pending_lease_seconds)
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.idempotency_records: dict[str, dict] = {}
        self._lock = RLock()

    def create_session(self, *, machine_id: str, owner_subject: str) -> SessionMetadata:
        metadata = _new_session_metadata(
            machine_id=machine_id,
            owner_subject=owner_subject,
            ttl_seconds=self.ttl_seconds,
        )
        session_id = metadata["sessionId"]
        self.sessions[session_id] = metadata
        self.messages[session_id] = []
        return self._metadata(metadata, message_count=0)

    def create_session_idempotent(
        self,
        *,
        machine_id: str,
        owner_subject: str,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
    ) -> SessionMetadata:
        record_key = _idempotency_key(scope, key)
        with self._lock:
            record = self.idempotency_records.get(record_key)
            if record is None or _parse_time(record["expiresAt"]) <= _now():
                self.idempotency_records.pop(record_key, None)
                raise IdempotencyInProgress(
                    "The idempotency lease expired before session creation."
                )
            _validate_idempotency_record(record, fingerprint=fingerprint)
            if record.get("ownerToken") != owner_token or record.get("status") != "pending":
                raise IdempotencyInProgress("The idempotency lease is owned by another request.")
            metadata = _new_session_metadata(
                machine_id=machine_id,
                owner_subject=owner_subject,
                ttl_seconds=self.ttl_seconds,
            )
            session_id = metadata["sessionId"]
            self.sessions[session_id] = metadata
            self.messages[session_id] = []
            response = self._metadata(metadata, message_count=0).to_dict()
            self.idempotency_records[record_key] = {
                "status": "completed",
                "fingerprint": fingerprint,
                "response": response,
                "expiresAt": _iso(_now() + timedelta(seconds=self.ttl_seconds)),
            }
            return self._metadata(metadata, message_count=0)

    def get_session(
        self,
        session_id: str,
        *,
        owner_subject: str,
        machine_id: str | None = None,
        include_messages: bool = False,
    ) -> ChatSession:
        metadata = self._load_metadata(session_id)
        self._authorize(metadata, owner_subject=owner_subject, machine_id=machine_id)
        metadata = self._refresh_metadata(session_id, metadata)
        messages = (
            [dict(item) for item in self.messages.get(session_id, [])] if include_messages else []
        )
        return ChatSession(
            metadata=self._metadata(metadata, message_count=len(self.messages.get(session_id, []))),
            messages=messages,
        )

    def append_message(
        self,
        session_id: str,
        *,
        owner_subject: str,
        machine_id: str,
        message: dict,
    ) -> SessionMetadata:
        metadata = self._load_metadata(session_id)
        self._authorize(metadata, owner_subject=owner_subject, machine_id=machine_id)
        metadata = self._refresh_metadata(session_id, metadata)
        self.messages.setdefault(session_id, []).append(dict(message))
        self.messages[session_id] = _bounded_messages(
            self.messages[session_id],
            max_messages=self.max_messages,
            max_bytes=self.max_bytes,
        )
        return self._metadata(metadata, message_count=len(self.messages[session_id]))

    def update_diagnostic_state(
        self,
        session_id: str,
        *,
        owner_subject: str,
        machine_id: str,
        diagnostic_state: dict | None,
    ) -> SessionMetadata:
        metadata = self._load_metadata(session_id)
        self._authorize(metadata, owner_subject=owner_subject, machine_id=machine_id)
        refreshed = self._refresh_metadata(
            session_id,
            {
                **metadata,
                "diagnosticState": diagnostic_state,
            },
        )
        return self._metadata(refreshed, message_count=len(self.messages.get(session_id, [])))

    def begin_idempotent_operation(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
    ) -> dict | None:
        record_key = _idempotency_key(scope, key)
        with self._lock:
            record = self.idempotency_records.get(record_key)
            if record and _parse_time(record["expiresAt"]) <= _now():
                self.idempotency_records.pop(record_key, None)
                record = None
            if record is None:
                self.idempotency_records[record_key] = {
                    "status": "pending",
                    "fingerprint": fingerprint,
                    "ownerToken": owner_token,
                    "expiresAt": _iso(_now() + timedelta(seconds=self.pending_lease_seconds)),
                }
                return None
            _validate_idempotency_record(record, fingerprint=fingerprint)
            if record.get("status") == "completed" and isinstance(record.get("response"), dict):
                return json.loads(json.dumps(record["response"]))
            raise IdempotencyInProgress("An operation with this idempotency key is in progress.")

    def complete_idempotent_operation(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
        response: dict,
    ) -> None:
        record_key = _idempotency_key(scope, key)
        with self._lock:
            existing = self.idempotency_records.get(record_key)
            if existing is None or _parse_time(existing["expiresAt"]) <= _now():
                self.idempotency_records.pop(record_key, None)
                raise IdempotencyInProgress("The idempotency lease expired before completion.")
            _validate_idempotency_record(existing, fingerprint=fingerprint)
            if existing.get("status") != "pending" or existing.get("ownerToken") != owner_token:
                raise IdempotencyInProgress("The idempotency lease is owned by another request.")
            self.idempotency_records[record_key] = {
                "status": "completed",
                "fingerprint": fingerprint,
                "response": json.loads(json.dumps(response)),
                "expiresAt": _iso(_now() + timedelta(seconds=self.ttl_seconds)),
            }

    def abort_idempotent_operation(
        self,
        *,
        scope: str,
        key: str,
        fingerprint: str,
        owner_token: str,
    ) -> None:
        record_key = _idempotency_key(scope, key)
        with self._lock:
            record = self.idempotency_records.get(record_key)
            if (
                record is not None
                and record.get("fingerprint") == fingerprint
                and record.get("ownerToken") == owner_token
                and record.get("status") == "pending"
            ):
                self.idempotency_records.pop(record_key, None)

    def _load_metadata(self, session_id: str) -> dict:
        metadata = self.sessions.get(session_id)
        if metadata is None or _parse_time(metadata["expiresAt"]) <= _now():
            self.sessions.pop(session_id, None)
            self.messages.pop(session_id, None)
            raise SessionNotFound("Session not found.")

        return metadata

    def _refresh_metadata(self, session_id: str, metadata: dict) -> dict:
        refreshed = {
            **metadata,
            "expiresAt": _iso(_now() + timedelta(seconds=self.ttl_seconds)),
        }
        self.sessions[session_id] = refreshed
        return refreshed

    @staticmethod
    def _authorize(metadata: dict, *, owner_subject: str, machine_id: str | None) -> None:
        if metadata["ownerSubject"] != owner_subject:
            raise SessionForbidden("Session belongs to a different authenticated subject.")

        if machine_id is not None and metadata["machineId"] != machine_id:
            raise SessionForbidden("Session belongs to a different machine.")

    @staticmethod
    def _metadata(metadata: dict, *, message_count: int) -> SessionMetadata:
        return SessionMetadata(
            session_id=metadata["sessionId"],
            machine_id=metadata["machineId"],
            owner_subject=metadata["ownerSubject"],
            created_at=metadata["createdAt"],
            expires_at=metadata["expiresAt"],
            message_count=message_count,
            diagnostic_state=metadata.get("diagnosticState"),
        )


def build_session_store(*, ttl_seconds: int) -> SessionStore:
    return InMemorySessionStore(ttl_seconds=ttl_seconds)


def _idempotency_key(scope: str, key: str) -> str:
    digest = sha256(f"{scope}\0{key}".encode("utf-8")).hexdigest()
    return f"idempotency:{digest}"


def _validate_idempotency_record(record: dict, *, fingerprint: str) -> None:
    if record.get("fingerprint") != fingerprint:
        raise IdempotencyConflict(
            "The idempotency key was already used with a different request payload."
        )


def _bounded_messages(
    messages: list[dict],
    *,
    max_messages: int,
    max_bytes: int,
) -> list[dict]:
    bounded = messages[-max_messages:]
    retained: list[dict] = []
    total_bytes = 0
    for message in reversed(bounded):
        size = len(json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        if retained and total_bytes + size > max_bytes:
            break
        retained.append(message)
        total_bytes += size
    return list(reversed(retained))


def _new_session_metadata(
    *,
    machine_id: str,
    owner_subject: str,
    ttl_seconds: int,
) -> dict:
    now = _now()
    return {
        "sessionId": str(uuid4()),
        "machineId": machine_id,
        "ownerSubject": owner_subject,
        "createdAt": _iso(now),
        "expiresAt": _iso(now + timedelta(seconds=ttl_seconds)),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
