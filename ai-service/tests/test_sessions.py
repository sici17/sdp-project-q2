from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from arol_ai.api import main as api_main
from arol_ai.sessions import (
    IdempotencyConflict,
    IdempotencyInProgress,
    InMemorySessionStore,
    SessionForbidden,
    SessionNotFound,
)


def test_in_memory_session_store_create_load_append_and_refresh_ttl() -> None:
    store = InMemorySessionStore(ttl_seconds=60)
    metadata = store.create_session(machine_id="MCH-0004", owner_subject="operator-1")

    loaded = store.get_session(
        metadata.session_id,
        owner_subject="operator-1",
        machine_id="MCH-0004",
        include_messages=True,
    )
    assert loaded.metadata.machine_id == "MCH-0004"
    assert loaded.messages == []

    updated = store.append_message(
        metadata.session_id,
        owner_subject="operator-1",
        machine_id="MCH-0004",
        message=_message("user", "hello"),
    )

    assert updated.message_count == 1
    assert datetime.fromisoformat(updated.expires_at) >= datetime.fromisoformat(metadata.expires_at)


def test_session_store_persists_diagnostic_state() -> None:
    store = InMemorySessionStore(ttl_seconds=60)
    metadata = store.create_session(machine_id="MCH-0004", owner_subject="operator-1")
    diagnostic_state = {
        "status": "active",
        "activeFault": "TORQUE_HIGH",
        "currentStepIndex": 0,
        "completedChecks": [],
        "failedChecks": [],
        "escalationState": "none",
        "steps": [{"label": "Record active alarm TORQUE_HIGH", "status": "pending"}],
    }

    updated = store.update_diagnostic_state(
        metadata.session_id,
        owner_subject="operator-1",
        machine_id="MCH-0004",
        diagnostic_state=diagnostic_state,
    )
    loaded = store.get_session(
        metadata.session_id,
        owner_subject="operator-1",
        machine_id="MCH-0004",
    )

    assert updated.diagnostic_state == diagnostic_state
    assert loaded.metadata.diagnostic_state == diagnostic_state
    assert loaded.to_dict()["diagnosticState"]["activeFault"] == "TORQUE_HIGH"


def test_session_store_rejects_cross_user_and_cross_machine_access() -> None:
    store = InMemorySessionStore(ttl_seconds=60)
    metadata = store.create_session(machine_id="MCH-0004", owner_subject="operator-1")

    try:
        store.get_session(metadata.session_id, owner_subject="operator-2")
    except SessionForbidden as exc:
        assert "different authenticated subject" in str(exc)
    else:
        raise AssertionError("Expected cross-user access to fail.")

    try:
        store.append_message(
            metadata.session_id,
            owner_subject="operator-1",
            machine_id="euro-vp-head-01",
            message=_message("user", "hello"),
        )
    except SessionForbidden as exc:
        assert "different machine" in str(exc)
    else:
        raise AssertionError("Expected cross-machine access to fail.")

    try:
        store.get_session("missing", owner_subject="operator-1")
    except SessionNotFound:
        pass
    else:
        raise AssertionError("Expected missing session to fail.")


def test_session_store_bounds_history_by_count_and_preserves_newest_message() -> None:
    store = InMemorySessionStore(
        ttl_seconds=60,
        max_messages=3,
        max_bytes=1024 * 1024,
    )
    metadata = store.create_session(
        machine_id="MCH-0004",
        owner_subject="operator-1",
    )

    for index in range(5):
        store.append_message(
            metadata.session_id,
            owner_subject="operator-1",
            machine_id="MCH-0004",
            message=_message("user", f"message-{index}"),
        )

    loaded = store.get_session(
        metadata.session_id,
        owner_subject="operator-1",
        include_messages=True,
    )
    assert loaded.metadata.message_count == 3
    assert [item["content"] for item in loaded.messages] == [
        "message-2",
        "message-3",
        "message-4",
    ]


def test_session_store_rejects_stale_idempotency_owner_after_lease_takeover() -> None:
    store = InMemorySessionStore(ttl_seconds=60, pending_lease_seconds=30)
    operation = {
        "scope": "operator-1:chat.complete:session-1",
        "key": "request-1",
        "fingerprint": "fingerprint-1",
    }

    assert store.begin_idempotent_operation(**operation, owner_token="owner-1") is None
    record = next(iter(store.idempotency_records.values()))
    record["expiresAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert store.begin_idempotent_operation(**operation, owner_token="owner-2") is None

    with pytest.raises(IdempotencyInProgress):
        store.complete_idempotent_operation(
            **operation,
            owner_token="owner-1",
            response={"message": "stale"},
        )

    store.complete_idempotent_operation(
        **operation,
        owner_token="owner-2",
        response={"message": "committed"},
    )
    assert store.begin_idempotent_operation(
        **operation,
        owner_token="owner-3",
    ) == {"message": "committed"}


def test_session_store_creates_session_and_replay_record_atomically() -> None:
    store = InMemorySessionStore(ttl_seconds=60)
    operation = {
        "scope": "operator-1:chat.sessions",
        "key": "create-1",
        "fingerprint": "fingerprint-1",
    }
    assert store.begin_idempotent_operation(**operation, owner_token="owner-1") is None

    metadata = store.create_session_idempotent(
        machine_id="MCH-0004",
        owner_subject="operator-1",
        **operation,
        owner_token="owner-1",
    )

    replay = store.begin_idempotent_operation(
        **operation,
        owner_token="owner-2",
    )
    assert replay == metadata.to_dict()
    assert list(store.sessions) == [metadata.session_id]

    with pytest.raises(IdempotencyConflict):
        store.begin_idempotent_operation(
            scope=operation["scope"],
            key=operation["key"],
            fingerprint="different-fingerprint",
            owner_token="owner-3",
        )


def test_ai_api_rejects_invalid_shared_secret(monkeypatch) -> None:
    client, _store = _configured_client(monkeypatch)

    response = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers={"X-Arol-Subject": "operator-1"},
    )

    assert response.status_code == 401


def test_ai_api_rejects_oversized_or_unsafe_identifiers(monkeypatch) -> None:
    client, _store = _configured_client(monkeypatch)
    headers = _headers(subject="operator-1")

    oversized_machine = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "m" * 129},
        headers=headers,
    )
    unsafe_machine = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "../MCH-0004"},
        headers=headers,
    )

    assert oversized_machine.status_code == 422
    assert unsafe_machine.status_code == 422


def test_ai_api_session_create_get_and_owner_enforcement(monkeypatch) -> None:
    client, _store = _configured_client(monkeypatch)
    headers = _headers(subject="operator-1")

    created = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers=headers,
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["machineId"] == "MCH-0004"
    assert payload["ownerSubject"] == "operator-1"
    assert payload["messageCount"] == 0

    loaded = client.get(f"/api/v1/chat/sessions/{payload['sessionId']}", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["messages"] == []

    forbidden = client.get(
        f"/api/v1/chat/sessions/{payload['sessionId']}",
        headers=_headers(subject="operator-2"),
    )
    assert forbidden.status_code == 403


def test_ai_api_session_create_idempotency_replays_same_session(monkeypatch) -> None:
    client, store = _configured_client(monkeypatch)
    headers = {
        **_headers(subject="operator-1"),
        "Idempotency-Key": "create-session-1",
    }

    first = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers=headers,
    )
    replay = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers=headers,
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert first.headers["Idempotency-Replayed"] == "false"
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(store.sessions) == 1


def test_ai_api_chat_uses_store_owned_history_and_ignores_client_history(monkeypatch) -> None:
    client, _store = _configured_client(monkeypatch)
    headers = _headers(subject="operator-1")
    captured: dict[str, list[dict]] = {}

    def fake_complete_chat(request: dict) -> dict:
        captured["messages"] = request["messages"]
        return {
            "message": _message("assistant", "grounded answer"),
            "agentTrace": ["supervisor"],
            "intents": ["conversation"],
            "routingReason": "test",
            "evidence": [],
            "toolCalls": [],
            "diagnosticSteps": [],
            "recommendedActions": [],
            "answerConfidence": 0.95,
            "reviewRequired": False,
            "reviewReasons": [],
        }

    monkeypatch.setattr(api_main, "complete_chat", fake_complete_chat)

    created = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers=headers,
    )
    session_id = created.json()["sessionId"]

    response = client.post(
        "/api/v1/chat/complete",
        json={
            "sessionId": session_id,
            "machineId": "MCH-0004",
            "message": "How are you?",
            "messages": [_message("user", "client-supplied history must be ignored")],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert [message["content"] for message in captured["messages"]] == ["How are you?"]

    session = client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers).json()
    assert session["messageCount"] == 2
    assert [message["role"] for message in session["messages"]] == ["user", "assistant"]


def test_ai_api_chat_idempotency_replays_without_duplicate_messages(monkeypatch) -> None:
    client, _store = _configured_client(monkeypatch)
    headers = _headers(subject="operator-1")
    calls = 0

    def fake_complete_chat(_request: dict) -> dict:
        nonlocal calls
        calls += 1
        return _chat_response(
            message="one grounded answer",
            agent_trace=["supervisor"],
            intents=["conversation"],
        )

    monkeypatch.setattr(api_main, "complete_chat", fake_complete_chat)
    created = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers=headers,
    )
    session_id = created.json()["sessionId"]
    request = {
        "sessionId": session_id,
        "machineId": "MCH-0004",
        "message": "Hello",
        "idempotencyKey": "complete-1",
    }

    first = client.post("/api/v1/chat/complete", json=request, headers=headers)
    replay = client.post("/api/v1/chat/complete", json=request, headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert calls == 1
    session = client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers).json()
    assert session["messageCount"] == 2


def test_ai_api_diagnostic_follow_up_uses_session_state(monkeypatch) -> None:
    client, _store = _configured_client(monkeypatch)
    headers = _headers(subject="operator-1")
    captured_messages: list[str] = []

    def fake_complete_chat(request: dict) -> dict:
        captured_messages.append(request["message"])
        if len(captured_messages) == 1:
            return _chat_response(
                message="first diagnostic answer",
                agent_trace=["supervisor", "doc-agent", "telemetry-agent", "troubleshooting-agent"],
                intents=["documentation", "telemetry", "troubleshooting"],
                evidence=[
                    {
                        "source": "telemetry",
                        "title": "Latest machine telemetry",
                        "excerpt": "Health warning; active alarm TORQUE_HIGH.",
                        "confidence": 0.88,
                    },
                    {
                        "source": "manual",
                        "title": "Torque adjustment",
                        "excerpt": "Check the torque setting.",
                        "page": 90,
                        "sourceUri": "/manuals/example.pdf",
                    },
                ],
                diagnostic_steps=[
                    {
                        "label": "Record active alarm TORQUE_HIGH",
                        "detail": "Capture telemetry.",
                        "priority": "immediate",
                        "requiresTechnician": False,
                        "source": "telemetry",
                    },
                    {
                        "label": "Follow manual section: Torque adjustment",
                        "detail": "Check the torque setting.",
                        "priority": "next",
                        "requiresTechnician": False,
                        "source": "manual",
                        "page": 90,
                    },
                ],
            )

        assert "Diagnostic follow-up for active fault TORQUE_HIGH" in request["message"]
        assert "still failing" in request["message"]
        assert request["messages"][-1]["content"] == "no"
        return _chat_response(
            message="follow-up diagnostic answer",
            agent_trace=["supervisor", "doc-agent", "telemetry-agent", "troubleshooting-agent"],
            intents=["documentation", "telemetry", "troubleshooting"],
            diagnostic_steps=[
                {
                    "label": "Follow manual section: Torque adjustment",
                    "detail": "Check the torque setting.",
                    "priority": "next",
                    "requiresTechnician": False,
                    "source": "manual",
                    "page": 90,
                },
                {
                    "label": "Escalate if the alarm remains",
                    "detail": "Send the telemetry snapshot to support.",
                    "priority": "escalate",
                    "requiresTechnician": True,
                    "source": "troubleshooting",
                },
            ],
            recommended_actions=[
                {
                    "label": "Escalate if the alarm persists after operator checks.",
                    "priority": "escalate",
                    "requiresTechnician": True,
                    "source": "telemetry",
                }
            ],
        )

    monkeypatch.setattr(api_main, "complete_chat", fake_complete_chat)
    created = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers=headers,
    )
    session_id = created.json()["sessionId"]

    first = client.post(
        "/api/v1/chat/complete",
        json={
            "sessionId": session_id,
            "machineId": "MCH-0004",
            "message": "The machine shows a torque alarm. What should I check?",
        },
        headers=headers,
    )
    assert first.status_code == 200

    after_first = client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers).json()
    assert after_first["diagnosticState"]["activeFault"] == "TORQUE_HIGH"
    assert after_first["diagnosticState"]["status"] == "active"

    second = client.post(
        "/api/v1/chat/complete",
        json={
            "sessionId": session_id,
            "machineId": "MCH-0004",
            "message": "no",
        },
        headers=headers,
    )
    assert second.status_code == 200

    after_second = client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers).json()
    diagnostic_state = after_second["diagnosticState"]
    assert diagnostic_state["failedChecks"] == ["Record active alarm TORQUE_HIGH"]
    assert diagnostic_state["currentStepIndex"] == 0
    assert diagnostic_state["steps"][0]["label"] == "Follow manual section: Torque adjustment"
    assert diagnostic_state["escalationState"] == "recommended"
    assert [message["content"] for message in after_second["messages"]][-2:] == [
        "no",
        "follow-up diagnostic answer",
    ]


def test_next_diagnostic_state_uses_failed_branch_target() -> None:
    previous = {
        "status": "active",
        "activeFault": "TORQUE_HIGH",
        "currentStepIndex": 0,
        "completedChecks": [],
        "failedChecks": [],
        "steps": [
            {
                "label": "Inspect the capping head and torque path",
                "status": "pending",
                "stepId": "torque-path-check",
                "failNextStepId": "escalate-persistent-alarm",
            }
        ],
    }
    response = _chat_response(
        message="branch follow-up",
        agent_trace=["supervisor", "troubleshooting-agent"],
        intents=["troubleshooting"],
        diagnostic_steps=[
            {
                "label": "Inspect the capping head and torque path",
                "detail": "Check chuck wear.",
                "priority": "next",
                "requiresTechnician": False,
                "source": "troubleshooting",
                "stepId": "torque-path-check",
                "failNextStepId": "escalate-persistent-alarm",
            },
            {
                "label": "Escalate if the alarm remains",
                "detail": "Send the telemetry snapshot to support.",
                "priority": "escalate",
                "requiresTechnician": True,
                "source": "troubleshooting",
                "stepId": "escalate-persistent-alarm",
            },
        ],
    )

    diagnostic_state = api_main._next_diagnostic_state(
        previous=previous,
        operator_message="no",
        diagnostic_signal="failed",
        response=response,
    )

    assert diagnostic_state["failedChecks"] == ["Inspect the capping head and torque path"]
    assert diagnostic_state["currentStepIndex"] == 1
    assert diagnostic_state["lastBranchStepId"] == "escalate-persistent-alarm"
    assert diagnostic_state["steps"][0]["status"] == "failed"
    assert diagnostic_state["steps"][1]["status"] == "pending"


def test_ai_api_rejects_cross_machine_chat_session_access(monkeypatch) -> None:
    client, _store = _configured_client(monkeypatch)
    headers = _headers(
        subject="operator-1",
        machine_ids="MCH-0004,euro-vp-head-01",
    )

    created = client.post(
        "/api/v1/chat/sessions",
        json={"machineId": "MCH-0004"},
        headers=headers,
    )
    session_id = created.json()["sessionId"]

    response = client.post(
        "/api/v1/chat/complete",
        json={
            "sessionId": session_id,
            "machineId": "euro-vp-head-01",
            "message": "Show telemetry status.",
        },
        headers=headers,
    )

    assert response.status_code == 403


def _configured_client(monkeypatch) -> tuple[TestClient, InMemorySessionStore]:
    settings = replace(
        api_main.settings,
        ai_service_shared_secret="shared-secret",
    )
    store = InMemorySessionStore(ttl_seconds=60)
    monkeypatch.setattr(api_main, "settings", settings)
    monkeypatch.setattr(api_main, "session_store", store)
    return TestClient(api_main.app), store


def _headers(
    *,
    subject: str,
    machine_ids: str = "MCH-0004",
) -> dict[str, str]:
    return {
        "X-Arol-Internal-Secret": "shared-secret",
        "X-Arol-Subject": subject,
        "X-Arol-Roles": "operator",
        "X-Arol-Machine-Ids": machine_ids,
    }


def _message(role: str, content: str) -> dict:
    return {
        "id": f"{role}-message",
        "role": role,
        "content": content,
        "createdAt": "2026-05-15T00:00:00+00:00",
    }


def _chat_response(
    *,
    message: str,
    agent_trace: list[str],
    intents: list[str],
    evidence: list[dict] | None = None,
    diagnostic_steps: list[dict] | None = None,
    recommended_actions: list[dict] | None = None,
) -> dict:
    return {
        "message": _message("assistant", message),
        "agentTrace": agent_trace,
        "intents": intents,
        "routingReason": "test",
        "evidence": evidence or [],
        "toolCalls": [],
        "diagnosticSteps": diagnostic_steps or [],
        "recommendedActions": recommended_actions or [],
        "answerConfidence": 0.8,
        "reviewRequired": False,
        "reviewReasons": [],
    }
