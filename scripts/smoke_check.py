"""Cross-platform end-to-end smoke checks for the running Compose stack."""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
AI_SERVICE_URL = os.environ.get("AI_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
MACHINE_ID = os.environ.get("MACHINE_ID", "MCH-0004")
#: The serial number is the key to the machine's own manual file.
MACHINE_SERIAL = os.environ.get("MACHINE_SERIAL", "17478")
SMOKE_GATEWAY_AUTH_MODE = os.environ.get("SMOKE_GATEWAY_AUTH_MODE", "demo").lower()
AI_SERVICE_SHARED_SECRET = os.environ.get(
    "AI_SERVICE_SHARED_SECRET", "local-only-change-me"
)
GATEWAY_OPENER = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
GATEWAY_CSRF_TOKEN = ""


def main() -> int:
    checks = [
        ("AI service health", check_ai_health),
        ("AI service readiness", check_ai_readiness),
        ("LangGraph definition", check_graph),
        ("AI manual index", check_ai_manual_index),
        ("Gateway health", check_gateway_health),
        ("Gateway readiness", check_gateway_readiness),
        ("Gateway authentication and CSRF", check_gateway_auth),
        ("Gateway machine context", check_machine_context),
        ("Gateway business data", check_business_data),
        ("Gateway manual search", check_manual_search),
        ("Gateway chat completion", check_chat_completion),
        ("Gateway streaming chat", check_chat_stream),
        ("Gateway metrics", check_metrics),
        ("AI metrics", check_ai_metrics),
        ("Gateway manual PDF", check_manual_pdf),
        ("Public QR and iframe path", check_public_frontend_origin),
    ]

    failures: list[str] = []
    for label, check in checks:
        try:
            check()
        except Exception as exc:  # pragma: no cover - exercised by deployment failures.
            failures.append(f"{label}: {exc}")
            print(f"[fail] {label}: {exc}")
        else:
            print(f"[ok] {label}")

    if failures:
        print("Smoke checks failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Smoke checks passed.")
    return 0


def check_ai_health() -> None:
    payload = get_json(f"{AI_SERVICE_URL}/health")
    expect(payload.get("status") == "ok", "AI service is not healthy")


def check_ai_readiness() -> None:
    payload = get_json(f"{AI_SERVICE_URL}/ready")
    expect(payload.get("status") == "ready", "AI service is not ready")


def check_graph() -> None:
    payload = get_json(f"{AI_SERVICE_URL}/api/v1/graph")
    expect("supervisor" in payload.get("nodes", []), "supervisor node is missing")
    expect(
        "troubleshooting-agent" in payload.get("nodes", []),
        "troubleshooting-agent node is missing",
    )
    expect(
        payload.get("checkpointing", {}).get("threadIdField") == "sessionId",
        "graph is not keyed by sessionId",
    )


def check_ai_manual_index() -> None:
    payload = get_json(f"{AI_SERVICE_URL}/api/v1/manuals/index/status")
    manuals = payload.get("manuals", [])
    expect(payload.get("status") == "indexed", "AI manual index is not indexed")
    expect(payload.get("totalIndexedChunks", 0) > 0, "AI manual index has no chunks")
    expect(len(manuals) >= 2, "expected at least two manifest manuals")
    for manual in manuals:
        expect(manual.get("pdfExists") is True, f"missing PDF: {manual.get('machineId')}")
        expect(
            manual.get("status") == "indexed" and (manual.get("indexedChunkCount") or 0) > 0,
            f"manual is not indexed: {manual.get('machineId')}",
        )


def check_gateway_health() -> None:
    payload = get_json(f"{GATEWAY_URL}/health")
    expect(payload.get("status") == "ok", "gateway is not healthy")


def check_gateway_readiness() -> None:
    payload = get_json(f"{GATEWAY_URL}/ready")
    expect(payload.get("status") == "ready", "gateway is not ready")
    dependencies = payload.get("dependencies", {})
    for dependency in ("aiService", "manuals", "operations"):
        expect(
            dependencies.get(dependency, {}).get("status") == "ready",
            f"gateway dependency is unavailable: {dependency}",
        )


def check_gateway_auth() -> None:
    global GATEWAY_CSRF_TOKEN

    if SMOKE_GATEWAY_AUTH_MODE == "off":
        payload = get_json(f"{GATEWAY_URL}/api/v1/auth/session")
        expect(payload.get("mode") == "off", "gateway auth is not in the expected off mode")
        return

    if SMOKE_GATEWAY_AUTH_MODE != "demo":
        raise RuntimeError(
            "SMOKE_GATEWAY_AUTH_MODE must be demo or off; OIDC requires an interactive identity"
        )

    payload = post_json(f"{GATEWAY_URL}/api/v1/auth/demo/login", {})
    GATEWAY_CSRF_TOKEN = str(payload.get("csrfToken") or "")
    expect(payload.get("mode") == "demo", "gateway demo auth did not start")
    expect(payload.get("authenticated") is True, "gateway demo auth is not authenticated")
    expect(bool(GATEWAY_CSRF_TOKEN), "gateway demo auth returned no CSRF token")
    expect("token" not in payload, "gateway demo auth exposed a bearer token")


def check_machine_context() -> None:
    payload = get_json(f"{GATEWAY_URL}/api/v1/machines/{MACHINE_ID}/context")
    expect(payload.get("machine", {}).get("id") == MACHINE_ID, "machine context ID mismatch")
    expect(payload.get("manual", {}).get("url"), "machine context has no manual URL")
    # Manuals are machine-specific and keyed by serial number, so the manual a
    # machine resolves to must be that machine's own.
    serial = payload.get("machine", {}).get("serialNumber")
    expect(serial, "machine context has no serial number")
    expect(
        f"{serial}_manual_EN.pdf" in payload.get("manual", {}).get("url", ""),
        "machine context manual does not match the machine serial number",
    )
    expect(
        payload.get("telemetry", {}).get("source") == "fleet-dataset",
        "telemetry source mismatch",
    )
    expect(
        payload.get("telemetry", {}).get("nominalRateBph"),
        "telemetry is missing the machine's own nominal rate",
    )


def check_business_data() -> None:
    contract = get_json(f"{GATEWAY_URL}/api/v1/machines/{MACHINE_ID}/contract")
    expect(contract.get("machineId") == MACHINE_ID, "contract machine ID mismatch")
    expect(contract.get("deliveryDate"), "contract delivery date is missing")
    expect(
        "coverageNote" in contract,
        "contract does not say why it carries no warranty status",
    )
    warranty = get_json(f"{GATEWAY_URL}/api/v1/machines/{MACHINE_ID}/warranty")
    expect(warranty.get("machineId") == MACHINE_ID, "warranty machine ID mismatch")
    orders = get_json(f"{GATEWAY_URL}/api/v1/machines/{MACHINE_ID}/orders")
    expect(bool(orders), "orders endpoint returned no records")
    history = get_json(f"{GATEWAY_URL}/api/v1/machines/{MACHINE_ID}/service-history")
    expect(bool(history), "service-history endpoint returned no records")


def check_manual_search() -> None:
    payload = post_json(
        f"{GATEWAY_URL}/api/v1/manuals/search",
        {"machineId": MACHINE_ID, "query": "closure head maintenance", "limit": 2},
    )
    evidence = payload.get("evidence", [])
    expect(payload.get("count", 0) > 0, "manual search returned no evidence")
    expect(any(item.get("sourceUri") for item in evidence), "manual evidence has no source URI")
    # A manual documents one physical machine, so every citation must come from
    # that machine's own document. Borrowing another machine's manual is the
    # failure that matters here, not a missing component manual.
    expect(
        all(
            MACHINE_SERIAL in str(item.get("sourceUri") or "")
            for item in evidence
        ),
        f"manual search cited a manual other than {MACHINE_SERIAL}'s",
    )
    expect(
        all(item.get("page") for item in evidence),
        "manual evidence carries no page number, so it cannot be looked up",
    )


def check_chat_completion() -> None:
    session_key = f"smoke-session-{uuid.uuid4().hex}"
    session = post_json(
        f"{GATEWAY_URL}/api/v1/chat/sessions",
        {"machineId": MACHINE_ID, "idempotencyKey": session_key},
        headers={"Idempotency-Key": session_key},
    )
    session_id = session.get("sessionId")
    expect(session_id, "chat session was not created")
    replayed_session = post_json(
        f"{GATEWAY_URL}/api/v1/chat/sessions",
        {"machineId": MACHINE_ID, "idempotencyKey": session_key},
        headers={"Idempotency-Key": session_key},
    )
    expect(
        replayed_session.get("sessionId") == session_id,
        "idempotent session retry created a duplicate session",
    )
    chat_key = f"smoke-chat-{uuid.uuid4().hex}"
    attachment = "Operator observed a torque rise after adjustment.".encode()
    payload = post_json(
        f"{GATEWAY_URL}/api/v1/chat/messages",
        {
            "sessionId": session_id,
            "machineId": MACHINE_ID,
            "message": "The machine shows a torque alarm. What should I check first?",
            "idempotencyKey": chat_key,
            "attachments": [
                {
                    "name": "operator-note.txt",
                    "type": "text/plain",
                    "size": len(attachment),
                    "contentBase64": base64.b64encode(attachment).decode("ascii"),
                }
            ],
        },
        headers={"Idempotency-Key": chat_key},
    )
    trace = payload.get("agentTrace", [])
    expect("doc-agent" in trace, "chat did not route to doc-agent")
    expect("telemetry-agent" in trace, "chat did not route to telemetry-agent")
    expect("troubleshooting-agent" in trace, "chat did not route to troubleshooting-agent")
    expect(any(item.get("source") == "manual" for item in payload.get("evidence", [])), "chat has no manual evidence")
    expect(payload.get("diagnosticSteps"), "chat has no diagnostic steps")


def check_chat_stream() -> None:
    session_key = f"smoke-stream-session-{uuid.uuid4().hex}"
    session = post_json(
        f"{GATEWAY_URL}/api/v1/chat/sessions",
        {"machineId": MACHINE_ID, "idempotencyKey": session_key},
        headers={"Idempotency-Key": session_key},
    )
    stream_key = f"smoke-stream-{uuid.uuid4().hex}"
    body = json.dumps(
        {
            "sessionId": session["sessionId"],
            "machineId": MACHINE_ID,
            "message": "The machine shows a torque alarm. What manual step applies?",
            "idempotencyKey": stream_key,
        }
    ).encode("utf-8")
    payload = request(
        f"{GATEWAY_URL}/api/v1/chat/stream",
        method="POST",
        body=body,
        headers={"Idempotency-Key": stream_key},
    )
    expect(b"event: trace" in payload, "stream has no trace event")
    expect(
        payload.count(b"event: token") >= 2,
        "stream did not progressively emit at least two token events",
    )
    expect(b"event: done" in payload, "stream has no done event")


def check_metrics() -> None:
    payload = request(f"{GATEWAY_URL}/metrics").decode("utf-8")
    expect("arol_gateway_http_requests_total" in payload, "HTTP metrics are missing")
    expect("arol_gateway_http_request_duration_ms_bucket" in payload, "HTTP duration histogram is missing")
    expect("arol_gateway_stream_first_token_duration_ms_bucket" in payload, "stream latency histogram is missing")
    expect("arol_gateway_expert_review_queue_open" in payload, "review metrics are missing")
    expect(
        'arol_gateway_operations_storage_info{mode="sqlite"} 1' in payload,
        "gateway is not using the SQLite operations store",
    )


def check_ai_metrics() -> None:
    payload = request(f"{AI_SERVICE_URL}/metrics").decode("utf-8")
    expect("arol_ai_http_request_duration_ms_bucket" in payload, "AI HTTP histogram is missing")
    expect("arol_ai_chat_completion_duration_ms_bucket" in payload, "AI chat histogram is missing")
    expect("arol_ai_rag_retrieval_duration_ms_bucket" in payload, "AI retrieval histogram is missing")


def check_manual_pdf() -> None:
    context = get_json(f"{GATEWAY_URL}/api/v1/machines/{MACHINE_ID}/context")
    url = f"{GATEWAY_URL}{context['manual']['url']}"
    response = request_response(url)
    expect(response.status == 200, "manual PDF request failed")
    expect(response.headers.get_content_type() == "application/pdf", "manual is not a PDF response")
    expect(response.headers.get("Accept-Ranges") == "bytes", "manual does not advertise byte ranges")
    total_size = int(response.headers.get("Content-Length") or 0)
    expect(total_size > 100, "manual Content-Length is missing or invalid")
    expect(response.headers.get("X-Frame-Options") is None, "manual PDF still denies all framing")
    expect(
        "frame-ancestors" in (response.headers.get("Content-Security-Policy") or ""),
        "manual PDF has no frame-ancestor policy",
    )
    ranged = request_response(url, headers={"Range": "bytes=0-99"})
    expect(ranged.status == 206, "manual byte-range request did not return 206")
    expect(
        ranged.headers.get("Content-Range") == f"bytes 0-99/{total_size}",
        "manual byte-range Content-Range is invalid",
    )
    expect(ranged.read().startswith(b"%PDF"), "manual byte range is not PDF content")


def check_public_frontend_origin() -> None:
    spa = request_response(f"{FRONTEND_URL}/m/{MACHINE_ID}")
    expect(spa.status == 200, "public QR route did not load")
    expect(spa.headers.get_content_type() == "text/html", "public QR route is not HTML")

    context = get_json(f"{FRONTEND_URL}/api/v1/machines/{MACHINE_ID}/context")
    expect(context.get("machine", {}).get("id") == MACHINE_ID, "public API proxy machine mismatch")
    manual = request_response(f"{FRONTEND_URL}{context['manual']['url']}")
    expect(manual.status == 200, "public manual proxy request failed")
    expect(manual.headers.get_content_type() == "application/pdf", "public manual is not a PDF")
    expect(
        manual.headers.get("X-Frame-Options") == "SAMEORIGIN",
        "public manual does not enforce same-origin framing",
    )
    expect(
        "frame-ancestors 'self'" in (manual.headers.get("Content-Security-Policy") or ""),
        "public manual CSP does not permit same-origin framing",
    )


def get_json(url: str) -> dict:
    return json.loads(request(url).decode("utf-8"))


def post_json(url: str, payload: dict, *, headers: dict[str, str] | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    return json.loads(
        request(url, method="POST", body=body, headers=headers).decode("utf-8")
    )


def request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    response = request_response(url, method=method, body=body, headers=headers)
    return response.read()


def request_response(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    request_headers = {"Accept": "application/json", **(headers or {})}
    if url.startswith(f"{AI_SERVICE_URL}/"):
        request_headers.update(
            {
                "X-Arol-Internal-Secret": AI_SERVICE_SHARED_SECRET,
                "X-Arol-Subject": "smoke-check",
                "X-Arol-Roles": "arol-support",
                "X-Arol-Machine-Ids": MACHINE_ID,
            }
        )
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if (
        GATEWAY_CSRF_TOKEN
        and method.upper() not in {"GET", "HEAD", "OPTIONS"}
        and (
            url.startswith(f"{GATEWAY_URL}/")
            or url.startswith(f"{FRONTEND_URL}/api/")
        )
    ):
        request_headers.setdefault("X-CSRF-Token", GATEWAY_CSRF_TOKEN)

    try:
        request_object = Request(
            url, data=body, headers=request_headers, method=method
        )
        if url.startswith(f"{GATEWAY_URL}/") or url.startswith(
            f"{FRONTEND_URL}/"
        ):
            return GATEWAY_OPENER.open(request_object, timeout=60)
        return urlopen(request_object, timeout=60)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:400]}") from exc
    except URLError as exc:
        raise RuntimeError(f"connection failed: {exc.reason}") from exc


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
