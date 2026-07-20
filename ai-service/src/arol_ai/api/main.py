import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arol_ai.access import ANONYMOUS, AccessContext, Domain
from arol_ai.agents.catalog import list_agent_capabilities
from arol_ai.api.attachments import (
    ChatAttachment,
    attachment_prompt_context,
    validate_attachment_collection,
)
from arol_ai.api.auth import (
    configured_internal_secrets,
    enforce_any_role,
    enforce_machine_access,
    enforce_tenancy,
    require_internal_auth,
)
from arol_ai.config import get_settings
from arol_ai.connectors.business import BusinessConnectorError
from arol_ai.data.manifest_repository import ManifestMachineRepository
from arol_ai.graph.orchestrator import (
    complete_chat,
    graph_definition,
    response_to_sse_events,
    sse_event,
    stream_chat_events,
)
from arol_ai.llm.providers import LLMProviderError, provider_readiness
from arol_ai.observability import metrics, reset_request_id, set_request_id, timed
from arol_ai.prompts.registry import MODEL_PROMPTS, describe_prompts, load_prompt
from arol_ai.rag.service import manual_index_status, search_manual_index
from arol_ai.sessions import (
    IdempotencyInProgress,
    SessionError,
    build_session_store,
)
from arol_ai.tools.registry import list_tool_capabilities
from arol_ai.workflows.escalation import EscalationDraftBuilder

app = FastAPI(title="AROL Q2 AI Service", version="0.1.0")
MAX_ORCHESTRATOR_HISTORY_MESSAGES = 24
MAX_ORCHESTRATOR_HISTORY_CHARS = 32_000
SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

settings = get_settings()
session_store = build_session_store(ttl_seconds=settings.session_ttl_seconds)
cors_origin = settings.cors_origin
if cors_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cors_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Request-Id",
            "X-Arol-Internal-Secret",
            "X-Arol-Subject",
            "X-Arol-Roles",
            "X-Arol-Machine-Ids",
            "Idempotency-Key",
        ],
    )


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    started_at = time.monotonic()
    request_id = request.headers.get("x-request-id", str(uuid4()))
    request_id_token = set_request_id(request_id)

    try:
        response = await call_next(request)
    except Exception:
        metrics.observe(
            "arol_ai_http_request_duration_ms",
            (time.monotonic() - started_at) * 1000,
            {"method": request.method, "path": request.url.path, "status": "500"},
        )
        raise
    finally:
        reset_request_id(request_id_token)

    duration_ms = (time.monotonic() - started_at) * 1000
    route = request.scope.get("route")
    metric_path = getattr(route, "path", None) or request.url.path
    response.headers["X-Request-Id"] = request_id
    metrics.observe(
        "arol_ai_http_request_duration_ms",
        duration_ms,
        {"method": request.method, "path": metric_path, "status": str(response.status_code)},
    )

    print(
        json.dumps(
            {
                "requestId": request_id,
                "method": request.method,
                "path": request.url.path,
                "statusCode": response.status_code,
                "durationMs": round(duration_ms),
            }
        )
    )

    return response


class ChatMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1, max_length=128, pattern=SAFE_IDENTIFIER_PATTERN)
    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1, max_length=8000)
    created_at: str = Field(alias="createdAt", min_length=1, max_length=64)


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_id: str = Field(
        alias="sessionId",
        min_length=1,
        max_length=128,
        pattern=SAFE_IDENTIFIER_PATTERN,
    )
    machine_id: str = Field(
        alias="machineId",
        min_length=1,
        max_length=128,
        pattern=SAFE_IDENTIFIER_PATTERN,
    )
    message: str = Field(min_length=1, max_length=2000)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=100)
    idempotency_key: str | None = Field(
        default=None,
        alias="idempotencyKey",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    attachments: list[ChatAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_attachments(self) -> "ChatRequest":
        validate_attachment_collection(self.attachments)
        return self

    def to_orchestrator_request(self, *, messages: list[dict] | None = None) -> dict:
        return {
            "sessionId": self.session_id,
            "machineId": self.machine_id,
            "message": self.message,
            "messages": messages
            if messages is not None
            else [
                {
                    "id": message.id,
                    "role": message.role,
                    "content": message.content,
                    "createdAt": message.created_at,
                }
                for message in self.messages
            ],
        }


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    machine_id: str = Field(
        alias="machineId",
        min_length=1,
        max_length=128,
        pattern=SAFE_IDENTIFIER_PATTERN,
    )
    idempotency_key: str | None = Field(
        default=None,
        alias="idempotencyKey",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class ManualSearchRequest(BaseModel):
    machine_id: str = Field(
        alias="machineId",
        min_length=1,
        max_length=128,
        pattern=SAFE_IDENTIFIER_PATTERN,
    )
    query: str = Field(min_length=1, max_length=2000)
    manual_version: str | None = Field(default=None, alias="manualVersion")
    language: str | None = None
    limit: int = Field(default=3, ge=1, le=10)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-service"}


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/ready")
def ready() -> dict:
    dependencies: dict[str, dict] = {
        "internalAuth": {
            "status": ("ready" if configured_internal_secrets(settings) else "unavailable"),
        },
        "sessions": {"status": "ready", "persistence": session_store.persistence_type},
    }

    repository = ManifestMachineRepository(settings)
    if settings.default_machine_id not in repository.list_machine_ids():
        dependencies["manifest"] = {"status": "unavailable"}
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "service": "ai-service",
                "dependencies": dependencies,
            },
        )

    dependencies["manifest"] = {"status": "ready"}

    if settings.doc_rag_enabled:
        index = manual_index_status(settings)
        dependencies["manualIndex"] = {
            "status": "ready" if index.get("status") == "indexed" else "unavailable",
            "collection": index.get("collection"),
            "totalIndexedChunks": index.get("totalIndexedChunks", 0),
            "expectedManualCount": index.get("expectedManualCount", 0),
            "indexedManualCount": index.get("indexedManualCount", 0),
            "unindexedManualIds": index.get("unindexedManualIds", []),
        }

    mcp_peers = (
        ("documentMcp", settings.doc_mcp_url, {"manual.search"}),
        (
            "telemetryMcp",
            settings.telemetry_service_url,
            {
                "telemetry.latest_snapshot",
                "telemetry.history",
                "telemetry.active_alarms",
                "telemetry.alarm_lookup",
            },
        ),
        (
            "businessMcp",
            settings.business_mcp_url,
            {
                "business.get_service_entitlement",
                "business.list_orders",
                "business.list_service_history",
            },
        ),
    )
    configured_peers = [
        (dependency_name, endpoint, required_tools)
        for dependency_name, endpoint, required_tools in mcp_peers
        if endpoint
    ]
    executor = ThreadPoolExecutor(max_workers=max(1, len(configured_peers)))
    futures = {
        executor.submit(_mcp_readiness, endpoint, required_tools): dependency_name
        for dependency_name, endpoint, required_tools in configured_peers
    }
    completed, pending = wait(futures, timeout=1.5)
    for future in completed:
        dependency_name = futures[future]
        try:
            dependencies[dependency_name] = future.result()
        except Exception:
            dependencies[dependency_name] = {
                "status": "unavailable",
                "error": "MCP readiness probe failed.",
            }
    for future in pending:
        dependencies[futures[future]] = {
            "status": "unavailable",
            "error": "MCP readiness probe timed out.",
        }
        future.cancel()
    executor.shutdown(wait=False, cancel_futures=True)
    dependencies["llmProvider"] = provider_readiness(settings, timeout_seconds=0.75)

    unavailable = [
        name for name, dependency in dependencies.items() if dependency["status"] != "ready"
    ]
    if unavailable:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "service": "ai-service",
                "dependencies": dependencies,
                "unavailable": unavailable,
            },
        )

    return {"status": "ready", "service": "ai-service", "dependencies": dependencies}


@app.get("/api/v1/agents")
def agents(http_request: Request) -> list[dict]:
    require_internal_auth(http_request, settings)
    return list_agent_capabilities()


@app.get("/api/v1/tools")
def tools(http_request: Request) -> list[dict]:
    require_internal_auth(http_request, settings)
    return list_tool_capabilities()


@app.get("/api/v1/graph")
def graph(http_request: Request) -> dict:
    require_internal_auth(http_request, settings)
    return graph_definition(session_persistence=session_store.persistence_type)


@app.get("/api/v1/prompts")
def prompts(http_request: Request) -> list[dict]:
    """Lists prompts, and says which are actually sent to a model.

    Only the answer synthesizer is. The rest specify what each agent may do,
    and those rules are enforced in code rather than by asking a model nicely.
    """
    require_internal_auth(http_request, settings)
    return describe_prompts()


@app.get("/api/v1/prompts/{prompt_name}")
def prompt(prompt_name: str, http_request: Request) -> dict[str, object]:
    auth_context = require_internal_auth(http_request, settings)
    enforce_any_role(auth_context, {"arol-admin", "arol-support", "admin", "support"})
    try:
        return {
            "name": prompt_name,
            "sentToModel": prompt_name in MODEL_PROMPTS,
            "template": load_prompt(prompt_name),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/machines")
def machines(http_request: Request) -> dict[str, list[str]]:
    auth_context = require_internal_auth(http_request, settings)
    machine_ids = ManifestMachineRepository(settings).list_machine_ids()
    if auth_context.has_machine_restriction:
        machine_ids = [
            machine_id for machine_id in machine_ids if machine_id in auth_context.machine_ids
        ]
    return {"machineIds": machine_ids}


@app.get("/api/v1/machines/{machine_id}/context")
def machine_context(machine_id: str, http_request: Request) -> dict:
    context = _authorized_machine_context(machine_id, http_request)

    if context.machine is None:
        raise HTTPException(status_code=404, detail="Machine not found.")

    return context.to_dict()


@app.get("/api/v1/machines/{machine_id}/contract")
def machine_contract(machine_id: str, http_request: Request) -> dict:
    context = _authorized_machine_context(
        machine_id,
        http_request,
        require_business=True,
        business_resources={"entitlement"},
    )
    if context.contract is None:
        raise HTTPException(status_code=404, detail="Contract not found.")
    return context.contract.to_dict()


@app.get("/api/v1/machines/{machine_id}/warranty")
def machine_warranty(machine_id: str, http_request: Request) -> dict:
    """The machine's service standing.

    Kept at ``/warranty`` because that is the question callers ask, but the
    answer says plainly that the dataset holds no warranty record instead of
    returning an empty status a caller would render as "not covered".
    """
    context = _authorized_machine_context(
        machine_id,
        http_request,
        require_business=True,
        business_resources={"entitlement"},
    )
    if context.contract is None:
        raise HTTPException(status_code=404, detail="Service record not found.")
    return context.contract.to_dict()


@app.get("/api/v1/machines/{machine_id}/orders")
def machine_orders(machine_id: str, http_request: Request) -> list[dict]:
    context = _authorized_machine_context(
        machine_id,
        http_request,
        require_business=True,
        business_resources={"orders"},
    )
    return [order.to_dict() for order in context.orders]


@app.get("/api/v1/machines/{machine_id}/service-history")
def machine_service_history(machine_id: str, http_request: Request) -> list[dict]:
    context = _authorized_machine_context(
        machine_id,
        http_request,
        require_business=True,
        business_resources={"service_history"},
    )
    return [record.to_dict() for record in context.service_history]


@app.get("/api/v1/manuals/index/status")
def manuals_index_status(http_request: Request) -> dict:
    require_internal_auth(http_request, settings)
    return manual_index_status(settings)


@app.post("/api/v1/manuals/search")
def manuals_search(request: ManualSearchRequest, http_request: Request) -> dict:
    auth_context = require_internal_auth(http_request, settings)
    enforce_machine_access(auth_context, request.machine_id)
    if not settings.doc_rag_enabled:
        raise HTTPException(status_code=503, detail="Document RAG is not enabled.")

    try:
        with timed("arol_ai_rag_retrieval_duration_ms", {"operation": "manual_search"}):
            evidence = search_manual_index(
                machine_id=request.machine_id,
                query=request.query,
                manual_version=request.manual_version,
                language=request.language,
                limit=request.limit,
                settings=settings,
            )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Manual index search failed: {exc}") from exc

    return {
        "machineId": request.machine_id,
        "query": request.query,
        "count": len(evidence),
        "evidence": [item.to_dict() for item in evidence],
    }


@app.post("/api/v1/chat/complete")
def chat_complete(
    chat_request: ChatRequest,
    http_request: Request,
    http_response: Response,
) -> dict:
    auth_context = require_internal_auth(http_request, settings)
    enforce_machine_access(auth_context, chat_request.machine_id)

    try:
        session = session_store.get_session(
            chat_request.session_id,
            owner_subject=auth_context.subject,
            machine_id=chat_request.machine_id,
            include_messages=True,
        )
        idempotency_key = _idempotency_key(chat_request.idempotency_key, http_request)
        scope = (
            f"{auth_context.subject}:chat.complete:{chat_request.session_id}"
            if idempotency_key
            else None
        )
        fingerprint = _chat_fingerprint(chat_request)
        owner_token = str(uuid4())
        cached = _begin_idempotent_operation(
            scope=scope,
            key=idempotency_key,
            fingerprint=fingerprint,
            owner_token=owner_token,
        )
        if cached is not None:
            http_response.headers["Idempotency-Replayed"] = "true"
            return cached
        if idempotency_key:
            http_response.headers["Idempotency-Replayed"] = "false"

        attachment_context = _attachment_context(chat_request)
        operation_id = _operation_id(scope, idempotency_key)
        user_message = _existing_operation_message(session.messages, operation_id)
        if user_message is None:
            user_message = _chat_message(
                "user",
                chat_request.message,
                attachments=chat_request.attachments,
                attachment_context=attachment_context,
                operation_id=operation_id,
            )
            session_store.append_message(
                chat_request.session_id,
                owner_subject=auth_context.subject,
                machine_id=chat_request.machine_id,
                message=user_message,
            )
        orchestrator_request, diagnostic_signal = _chat_orchestrator_request(
            chat_request,
            messages=_orchestrator_messages(session.messages, user_message),
            diagnostic_state=session.metadata.diagnostic_state,
            attachment_context=attachment_context,
            access=auth_context.access,
        )
        recovered = _existing_operation_response(session.messages, operation_id)
        if recovered is not None:
            _persist_chat_result(
                chat_request=chat_request,
                owner_subject=auth_context.subject,
                previous_diagnostic_state=session.metadata.diagnostic_state,
                diagnostic_signal=diagnostic_signal,
                result=recovered,
                operation_id=operation_id,
            )
            _complete_idempotent_operation(
                scope=scope,
                key=idempotency_key,
                fingerprint=fingerprint,
                owner_token=owner_token,
                response=recovered,
            )
            http_response.headers["Idempotency-Replayed"] = "true"
            return recovered
        with timed("arol_ai_chat_completion_duration_ms", {"mode": "complete"}):
            result = complete_chat(orchestrator_request)
        _persist_chat_result(
            chat_request=chat_request,
            owner_subject=auth_context.subject,
            previous_diagnostic_state=session.metadata.diagnostic_state,
            diagnostic_signal=diagnostic_signal,
            result=result,
            operation_id=operation_id,
        )
        _complete_idempotent_operation(
            scope=scope,
            key=idempotency_key,
            fingerprint=fingerprint,
            owner_token=owner_token,
            response=result,
        )
        return result
    except LLMProviderError as exc:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise HTTPException(status_code=503, detail=f"LLM synthesis failed: {exc}") from exc
    except ValueError as exc:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    except Exception:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise


@app.post("/api/v1/chat/stream")
def chat_stream(chat_request: ChatRequest, http_request: Request) -> StreamingResponse:
    auth_context = require_internal_auth(http_request, settings)
    enforce_machine_access(auth_context, chat_request.machine_id)

    try:
        session = session_store.get_session(
            chat_request.session_id,
            owner_subject=auth_context.subject,
            machine_id=chat_request.machine_id,
            include_messages=True,
        )
        idempotency_key = _idempotency_key(chat_request.idempotency_key, http_request)
        scope = (
            f"{auth_context.subject}:chat.stream:{chat_request.session_id}"
            if idempotency_key
            else None
        )
        fingerprint = _chat_fingerprint(chat_request)
        owner_token = str(uuid4())
        cached = _begin_idempotent_operation(
            scope=scope,
            key=idempotency_key,
            fingerprint=fingerprint,
            owner_token=owner_token,
        )
        if cached is not None:
            return StreamingResponse(
                response_to_sse_events(cached),
                media_type="text/event-stream",
                headers=_stream_headers(replayed=True),
            )

        attachment_context = _attachment_context(chat_request)
        operation_id = _operation_id(scope, idempotency_key)
        user_message = _existing_operation_message(session.messages, operation_id)
        if user_message is None:
            user_message = _chat_message(
                "user",
                chat_request.message,
                attachments=chat_request.attachments,
                attachment_context=attachment_context,
                operation_id=operation_id,
            )
            session_store.append_message(
                chat_request.session_id,
                owner_subject=auth_context.subject,
                machine_id=chat_request.machine_id,
                message=user_message,
            )
        orchestrator_request, diagnostic_signal = _chat_orchestrator_request(
            chat_request,
            messages=_orchestrator_messages(session.messages, user_message),
            diagnostic_state=session.metadata.diagnostic_state,
            attachment_context=attachment_context,
            access=auth_context.access,
        )
        recovered = _existing_operation_response(session.messages, operation_id)
        if recovered is not None:
            _persist_chat_result(
                chat_request=chat_request,
                owner_subject=auth_context.subject,
                previous_diagnostic_state=session.metadata.diagnostic_state,
                diagnostic_signal=diagnostic_signal,
                result=recovered,
                operation_id=operation_id,
            )
            _complete_idempotent_operation(
                scope=scope,
                key=idempotency_key,
                fingerprint=fingerprint,
                owner_token=owner_token,
                response=recovered,
            )
            return StreamingResponse(
                response_to_sse_events(recovered),
                media_type="text/event-stream",
                headers=_stream_headers(replayed=True),
            )

        def generate_events():
            completed = False
            try:
                with timed("arol_ai_chat_completion_duration_ms", {"mode": "stream"}):
                    for event_name, payload in stream_chat_events(orchestrator_request):
                        if event_name == "done":
                            _persist_chat_result(
                                chat_request=chat_request,
                                owner_subject=auth_context.subject,
                                previous_diagnostic_state=session.metadata.diagnostic_state,
                                diagnostic_signal=diagnostic_signal,
                                result=payload,
                                operation_id=operation_id,
                            )
                            _complete_idempotent_operation(
                                scope=scope,
                                key=idempotency_key,
                                fingerprint=fingerprint,
                                owner_token=owner_token,
                                response=payload,
                            )
                            completed = True
                        yield sse_event(event_name, payload)
            except LLMProviderError:
                yield sse_event(
                    "error",
                    {
                        "code": "llm_provider_failed",
                        "message": "The configured LLM provider failed during synthesis.",
                    },
                )
            except Exception:
                yield sse_event(
                    "error",
                    {
                        "code": "stream_failed",
                        "message": "The chat stream could not be completed.",
                    },
                )
            finally:
                if not completed:
                    _abort_idempotent_operation(
                        scope=scope,
                        key=idempotency_key,
                        fingerprint=fingerprint,
                        owner_token=owner_token,
                    )

        return StreamingResponse(
            generate_events(),
            media_type="text/event-stream",
            headers=_stream_headers(replayed=False),
        )
    except ValueError as exc:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    except Exception:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise


@app.post("/api/v1/chat/sessions", status_code=201)
def create_chat_session(
    request: SessionCreateRequest,
    http_request: Request,
    http_response: Response,
) -> dict:
    auth_context = require_internal_auth(http_request, settings)
    enforce_machine_access(auth_context, request.machine_id)

    if request.machine_id not in ManifestMachineRepository(settings).list_machine_ids():
        raise HTTPException(status_code=404, detail="Machine not found.")

    idempotency_key = _idempotency_key(request.idempotency_key, http_request)
    scope = f"{auth_context.subject}:chat.sessions" if idempotency_key else None
    fingerprint = _request_fingerprint({"machineId": request.machine_id})
    owner_token = str(uuid4())
    try:
        cached = _begin_idempotent_operation(
            scope=scope,
            key=idempotency_key,
            fingerprint=fingerprint,
            owner_token=owner_token,
        )
        if cached is not None:
            http_response.headers["Idempotency-Replayed"] = "true"
            return cached
        if idempotency_key:
            http_response.headers["Idempotency-Replayed"] = "false"

        if scope and idempotency_key:
            metadata = session_store.create_session_idempotent(
                machine_id=request.machine_id,
                owner_subject=auth_context.subject,
                scope=scope,
                key=idempotency_key,
                fingerprint=fingerprint,
                owner_token=owner_token,
            )
        else:
            metadata = session_store.create_session(
                machine_id=request.machine_id,
                owner_subject=auth_context.subject,
            )
        result = metadata.to_dict()
        return result
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    except Exception:
        _abort_idempotent_operation(
            scope=scope,
            key=idempotency_key,
            fingerprint=fingerprint,
            owner_token=owner_token,
        )
        raise


@app.get("/api/v1/chat/sessions/{session_id}")
def get_chat_session(session_id: str, http_request: Request) -> dict:
    auth_context = require_internal_auth(http_request, settings)

    try:
        session = session_store.get_session(
            session_id,
            owner_subject=auth_context.subject,
            include_messages=True,
        )
    except SessionError as exc:
        raise _session_http_error(exc) from exc

    enforce_machine_access(auth_context, session.metadata.machine_id)
    return session.to_dict()


@app.post("/api/v1/escalations/draft")
def escalation_draft(
    chat_request: ChatRequest,
    http_request: Request,
    http_response: Response,
) -> dict:
    auth_context = require_internal_auth(http_request, settings)
    enforce_machine_access(auth_context, chat_request.machine_id)

    repository = ManifestMachineRepository(settings)
    builder = EscalationDraftBuilder(repository)

    try:
        session = session_store.get_session(
            chat_request.session_id,
            owner_subject=auth_context.subject,
            machine_id=chat_request.machine_id,
            include_messages=True,
        )
        idempotency_key = _idempotency_key(chat_request.idempotency_key, http_request)
        scope = (
            f"{auth_context.subject}:escalations.draft:{chat_request.session_id}"
            if idempotency_key
            else None
        )
        fingerprint = _chat_fingerprint(chat_request)
        owner_token = str(uuid4())
        cached = _begin_idempotent_operation(
            scope=scope,
            key=idempotency_key,
            fingerprint=fingerprint,
            owner_token=owner_token,
        )
        if cached is not None:
            http_response.headers["Idempotency-Replayed"] = "true"
            return cached
        if idempotency_key:
            http_response.headers["Idempotency-Replayed"] = "false"

        request = chat_request.to_orchestrator_request(
            messages=_orchestrator_messages(session.messages),
        )
        request["message"] += _attachment_context(chat_request)
        result = builder.build(request)
        _complete_idempotent_operation(
            scope=scope,
            key=idempotency_key,
            fingerprint=fingerprint,
            owner_token=owner_token,
            response=result,
        )
        return result
    except LLMProviderError as exc:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise HTTPException(status_code=503, detail=f"LLM synthesis failed: {exc}") from exc
    except ValueError as exc:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    except Exception:
        _abort_idempotent_operation(
            scope=locals().get("scope"),
            key=locals().get("idempotency_key"),
            fingerprint=locals().get("fingerprint"),
            owner_token=locals().get("owner_token"),
        )
        raise


def _chat_message(
    role: Literal["user", "assistant", "system", "tool"],
    content: str,
    *,
    attachments: list[ChatAttachment] | None = None,
    attachment_context: str = "",
    operation_id: str | None = None,
) -> dict:
    message = {
        "id": str(uuid4()),
        "role": role,
        "content": content,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    if attachments:
        message["attachments"] = [attachment.safe_metadata() for attachment in attachments]
    if attachment_context:
        message["_attachmentContext"] = attachment_context
    if operation_id:
        message["_operationId"] = operation_id
    return message


def _chat_orchestrator_request(
    chat_request: ChatRequest,
    *,
    messages: list[dict],
    diagnostic_state: dict | None,
    attachment_context: str = "",
    access: AccessContext = ANONYMOUS,
) -> tuple[dict, str | None]:
    request = chat_request.to_orchestrator_request(messages=messages)
    # The agents enforce the access model on their own tools, so the identity
    # travels with the request rather than being checked only at this edge.
    request["access"] = access
    diagnostic_signal = _diagnostic_signal(diagnostic_state, chat_request.message)
    if diagnostic_signal:
        request["message"] = _diagnostic_follow_up_message(
            diagnostic_state or {},
            operator_message=chat_request.message,
            diagnostic_signal=diagnostic_signal,
        )
    request["message"] += attachment_context
    return request, diagnostic_signal


def _authorized_machine_context(
    machine_id: str,
    http_request: Request,
    *,
    require_business: bool = False,
    business_resources: set[str] | None = None,
):
    auth_context = require_internal_auth(http_request, settings)
    enforce_machine_access(auth_context, machine_id)
    repository = ManifestMachineRepository(settings)
    identity = repository.get_machine_context(
        machine_id,
        include_business=False,
        include_telemetry=False,
    )
    if identity.machine is None:
        raise HTTPException(status_code=404, detail="Machine not found.")
    enforce_tenancy(auth_context, identity.machine.company_id, machine_id)
    resources = business_resources or {"entitlement", "orders", "quotes", "service_history"}
    allowed = {
        resource
        for resource in resources
        if auth_context.access.can_see(
            Domain.OPERATIONAL if resource == "service_history" else Domain.COMMERCIAL
        )
    }
    if business_resources and allowed != resources:
        raise HTTPException(status_code=403, detail="Your role does not include this data.")
    try:
        context = repository.get_machine_context(
            machine_id,
            require_business=require_business,
            include_business=bool(allowed),
            include_telemetry=auth_context.access.can_see(Domain.OPERATIONAL),
            business_resources=allowed,
        )
    except BusinessConnectorError as exc:
        raise HTTPException(status_code=503, detail="Business data source is unavailable.") from exc
    if context.machine is None:
        raise HTTPException(status_code=404, detail="Machine not found.")

    # The tenant boundary is re-checked here rather than assumed from the
    # gateway, so a direct call to this service cannot reach another company's
    # machine even if it presents the internal secret.
    enforce_tenancy(auth_context, getattr(context.machine, "company_id", None), machine_id)
    if context.contract and not auth_context.access.can_see(Domain.OPERATIONAL):
        context = replace(
            context,
            contract=replace(
                context.contract,
                open_ticket_count=None,
                ticket_count=None,
                last_scheduled_maintenance=None,
            ),
        )
    return context


def _mcp_readiness(endpoint: str, required_tools: set[str]) -> dict:
    base_url = endpoint.rstrip("/")
    if base_url.endswith("/mcp"):
        base_url = base_url.removesuffix("/mcp")
    headers = {"Accept": "application/json"}
    if settings.mcp_shared_secret:
        headers["X-Arol-Mcp-Secret"] = settings.mcp_shared_secret
    try:
        request = UrlRequest(f"{base_url}/ready", headers=headers)
        with urlopen(request, timeout=1.1) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tool_names = {name for name in payload.get("tools", []) if isinstance(name, str)}
        missing_tools = sorted(required_tools - tool_names)
        if payload.get("status") != "ready" or missing_tools:
            return {
                "status": "unavailable",
                "endpoint": endpoint,
                "error": (
                    f"Required MCP tools were not ready: {', '.join(missing_tools)}."
                    if missing_tools
                    else "MCP peer reported not ready."
                ),
            }
        return {
            "status": "ready",
            "endpoint": endpoint,
            "requiredTools": sorted(required_tools),
        }
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "unavailable",
            "endpoint": endpoint,
            "error": "MCP readiness endpoint is unavailable.",
        }


def _attachment_context(chat_request: ChatRequest) -> str:
    try:
        return attachment_prompt_context(chat_request.attachments)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _idempotency_key(body_key: str | None, http_request: Request) -> str | None:
    header_key = http_request.headers.get("idempotency-key")
    if header_key is not None:
        header_key = header_key.strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", header_key):
            raise HTTPException(status_code=400, detail="Invalid Idempotency-Key header.")
    if body_key and header_key and body_key != header_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header and idempotencyKey body field must match.",
        )
    return header_key or body_key


def _chat_fingerprint(chat_request: ChatRequest) -> str:
    return _request_fingerprint(
        {
            "sessionId": chat_request.session_id,
            "machineId": chat_request.machine_id,
            "message": chat_request.message,
            "attachments": [
                {
                    **attachment.safe_metadata(),
                    "sha256": sha256(attachment.decoded_bytes()).hexdigest(),
                }
                for attachment in chat_request.attachments
            ],
        }
    )


def _request_fingerprint(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _begin_idempotent_operation(
    *,
    scope: str | None,
    key: str | None,
    fingerprint: str,
    owner_token: str,
) -> dict | None:
    if not scope or not key:
        return None
    return session_store.begin_idempotent_operation(
        scope=scope,
        key=key,
        fingerprint=fingerprint,
        owner_token=owner_token,
    )


def _complete_idempotent_operation(
    *,
    scope: str | None,
    key: str | None,
    fingerprint: str,
    owner_token: str,
    response: dict,
) -> None:
    if not scope or not key:
        return
    session_store.complete_idempotent_operation(
        scope=scope,
        key=key,
        fingerprint=fingerprint,
        owner_token=owner_token,
        response=response,
    )


def _abort_idempotent_operation(
    *,
    scope: str | None,
    key: str | None,
    fingerprint: str | None,
    owner_token: str | None,
) -> None:
    if not scope or not key or not fingerprint or not owner_token:
        return
    session_store.abort_idempotent_operation(
        scope=scope,
        key=key,
        fingerprint=fingerprint,
        owner_token=owner_token,
    )


def _operation_id(scope: str | None, key: str | None) -> str | None:
    if not scope or not key:
        return None
    return sha256(f"{scope}\0{key}".encode("utf-8")).hexdigest()


def _existing_operation_message(
    messages: list[dict],
    operation_id: str | None,
) -> dict | None:
    if operation_id is None:
        return None
    return next(
        (
            message
            for message in reversed(messages)
            if message.get("role") == "user" and message.get("_operationId") == operation_id
        ),
        None,
    )


def _existing_operation_response(
    messages: list[dict],
    operation_id: str | None,
) -> dict | None:
    if operation_id is None:
        return None
    return next(
        (
            message["_idempotencyResponse"]
            for message in reversed(messages)
            if message.get("role") == "assistant"
            and message.get("_operationId") == operation_id
            and isinstance(message.get("_idempotencyResponse"), dict)
        ),
        None,
    )


def _orchestrator_messages(
    messages: list[dict],
    user_message: dict | None = None,
) -> list[dict]:
    combined = list(messages)
    if user_message is not None and not any(
        message.get("id") == user_message.get("id") for message in combined
    ):
        combined.append(user_message)
    sanitized: list[dict] = []
    for message in combined:
        public_message = {key: value for key, value in message.items() if not key.startswith("_")}
        if message.get("_attachmentContext"):
            public_message["content"] = (
                str(public_message.get("content") or "") + message["_attachmentContext"]
            )
        sanitized.append(public_message)

    retained: list[dict] = []
    used_chars = 0
    for message in reversed(sanitized):
        message_chars = len(str(message.get("content") or ""))
        if retained and (
            len(retained) >= MAX_ORCHESTRATOR_HISTORY_MESSAGES
            or used_chars + message_chars > MAX_ORCHESTRATOR_HISTORY_CHARS
        ):
            break
        retained.append(message)
        used_chars += message_chars
    return list(reversed(retained))


def _persist_chat_result(
    *,
    chat_request: ChatRequest,
    owner_subject: str,
    previous_diagnostic_state: dict | None,
    diagnostic_signal: str | None,
    result: dict,
    operation_id: str | None,
) -> None:
    current = session_store.get_session(
        chat_request.session_id,
        owner_subject=owner_subject,
        machine_id=chat_request.machine_id,
        include_messages=True,
    )
    existing_assistant = next(
        (
            message
            for message in reversed(current.messages)
            if operation_id
            and message.get("role") == "assistant"
            and message.get("_operationId") == operation_id
        ),
        None,
    )
    diagnostic_state_after = (
        existing_assistant.get("_diagnosticStateAfter")
        if existing_assistant is not None
        else _next_diagnostic_state(
            previous=previous_diagnostic_state,
            operator_message=chat_request.message,
            diagnostic_signal=diagnostic_signal,
            response=result,
        )
    )
    if existing_assistant is None:
        assistant_message = dict(result["message"])
        if operation_id:
            assistant_message["_operationId"] = operation_id
            assistant_message["_idempotencyResponse"] = result
            assistant_message["_diagnosticStateAfter"] = diagnostic_state_after
        session_store.append_message(
            chat_request.session_id,
            owner_subject=owner_subject,
            machine_id=chat_request.machine_id,
            message=assistant_message,
        )
    session_store.update_diagnostic_state(
        chat_request.session_id,
        owner_subject=owner_subject,
        machine_id=chat_request.machine_id,
        diagnostic_state=diagnostic_state_after,
    )


def _stream_headers(*, replayed: bool) -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Idempotency-Replayed": "true" if replayed else "false",
    }


def _diagnostic_signal(diagnostic_state: dict | None, message: str) -> str | None:
    if not diagnostic_state or diagnostic_state.get("status") != "active":
        return None

    normalized = _normalized_follow_up(message)
    if not normalized:
        return None

    if any(phrase in normalized for phrase in ("fixed", "resolved", "cleared", "alarm cleared")):
        return "resolved"

    if normalized in {"no", "nope"} or any(
        phrase in normalized
        for phrase in (
            "still failing",
            "still present",
            "alarm remains",
            "not fixed",
            "same alarm",
            "issue remains",
            "did not work",
        )
    ):
        return "failed"

    if normalized in {
        "done",
        "checked",
        "checked it",
        "i checked it",
        "complete",
        "completed",
    } or any(phrase in normalized for phrase in ("i checked", "i completed", "done checking")):
        return "completed"

    return None


def _diagnostic_follow_up_message(
    diagnostic_state: dict,
    *,
    operator_message: str,
    diagnostic_signal: str,
) -> str:
    fault = diagnostic_state.get("activeFault") or "the active diagnostic"
    current_step = _current_diagnostic_step(diagnostic_state)
    current_label = current_step.get("label") if current_step else "the current diagnostic step"

    if diagnostic_signal == "failed":
        return (
            f"Diagnostic follow-up for active fault {fault}: the operator replied "
            f"{operator_message!r}, meaning the issue is still failing after {current_label}. "
            "Continue troubleshooting with the next safe diagnostic step using manual evidence and telemetry."
        )

    if diagnostic_signal == "completed":
        return (
            f"Diagnostic follow-up for active fault {fault}: the operator completed {current_label}. "
            "Continue troubleshooting with the next safe diagnostic step using manual evidence and telemetry."
        )

    return (
        f"Diagnostic follow-up for active fault {fault}: the operator reports the issue is resolved "
        f"after {current_label}. Summarize the resolved diagnostic and any restart or monitoring checks."
    )


def _next_diagnostic_state(
    *,
    previous: dict | None,
    operator_message: str,
    diagnostic_signal: str | None,
    response: dict,
) -> dict | None:
    response_steps = response.get("diagnosticSteps") or []
    if not previous and not response_steps:
        return None

    now = datetime.now(timezone.utc).isoformat()
    completed_checks = list((previous or {}).get("completedChecks") or [])
    failed_checks = list((previous or {}).get("failedChecks") or [])
    status = (previous or {}).get("status") or "active"
    current_step = _current_diagnostic_step(previous or {})
    branch_target_step_id = None

    if diagnostic_signal == "completed" and current_step:
        _append_unique(completed_checks, current_step["label"])
        branch_target_step_id = current_step.get("passNextStepId")
    elif diagnostic_signal == "failed":
        failed_label = current_step["label"] if current_step else operator_message
        _append_unique(failed_checks, failed_label)
        branch_target_step_id = current_step.get("failNextStepId") if current_step else None
    elif diagnostic_signal == "resolved":
        status = "resolved"
        if current_step:
            _append_unique(completed_checks, current_step["label"])

    if not response_steps:
        updated = dict(previous or {})
        updated.update(
            {
                "status": status,
                "completedChecks": completed_checks,
                "failedChecks": failed_checks,
                "updatedAt": now,
                "lastOperatorSignal": diagnostic_signal,
                "lastBranchStepId": branch_target_step_id,
            }
        )
        return updated

    step_states = _diagnostic_step_states(response_steps, completed_checks, failed_checks)
    current_step_index = _branch_step_index(step_states, branch_target_step_id)
    if current_step_index is None:
        current_step_index = _first_pending_step_index(step_states)
    escalation_state = _escalation_state(response=response, steps=step_states)
    if diagnostic_signal == "failed" and current_step_index is None:
        escalation_state = "recommended"

    return {
        "status": status if status == "resolved" else "active",
        "activeFault": _active_fault(
            response=response,
            previous=previous,
            fallback=operator_message,
        ),
        "currentStepIndex": (
            current_step_index if current_step_index is not None else max(len(step_states) - 1, 0)
        ),
        "completedChecks": completed_checks,
        "failedChecks": failed_checks,
        "escalationState": escalation_state,
        "selectedManualCitation": _selected_manual_citation(response),
        "lastTelemetrySnapshot": _last_telemetry_snapshot(response),
        "steps": step_states,
        "updatedAt": now,
        "lastOperatorSignal": diagnostic_signal,
        "lastBranchStepId": branch_target_step_id,
    }


def _diagnostic_step_states(
    response_steps: list[dict],
    completed_checks: list[str],
    failed_checks: list[str],
) -> list[dict]:
    completed = set(completed_checks)
    failed = set(failed_checks)
    steps = []
    for index, step in enumerate(response_steps):
        label = step.get("label", f"Step {index + 1}")
        status = "pending"
        if label in completed:
            status = "completed"
        elif label in failed:
            status = "failed"

        state = {
            "label": label,
            "detail": step.get("detail", ""),
            "priority": step.get("priority", "next"),
            "requiresTechnician": bool(step.get("requiresTechnician")),
            "source": step.get("source", "troubleshooting"),
            "status": status,
        }
        for key in (
            "page",
            "expectedOutcome",
            "passFollowUp",
            "failFollowUp",
            "safetyLevel",
            "requiredRole",
            "stepId",
            "passNextStepId",
            "failNextStepId",
        ):
            if step.get(key) is not None:
                state[key] = step[key]

        steps.append(state)
    return steps


def _branch_step_index(steps: list[dict], step_id: str | None) -> int | None:
    if not step_id:
        return None

    return next(
        (
            index
            for index, step in enumerate(steps)
            if step.get("stepId") == step_id and step.get("status") == "pending"
        ),
        None,
    )


def _first_pending_step_index(steps: list[dict]) -> int | None:
    for index, step in enumerate(steps):
        if step.get("status") == "pending":
            return index
    return None


def _current_diagnostic_step(diagnostic_state: dict) -> dict | None:
    steps = diagnostic_state.get("steps") or []
    if not steps:
        return None

    index = diagnostic_state.get("currentStepIndex", 0)
    if not isinstance(index, int) or index < 0 or index >= len(steps):
        index = 0
    return steps[index]


def _active_fault(*, response: dict, previous: dict | None, fallback: str) -> str:
    for step in response.get("diagnosticSteps") or []:
        match = re.search(r"active alarm\s+([A-Z0-9_ -]+)", step.get("label", ""), re.I)
        if match:
            return match.group(1).strip()

    for evidence in response.get("evidence") or []:
        if evidence.get("source") != "telemetry":
            continue
        match = re.search(r"active alarm\s+([A-Z0-9_ -]+)", evidence.get("excerpt", ""), re.I)
        if match:
            return match.group(1).strip().rstrip(".")

    if previous and previous.get("activeFault"):
        return str(previous["activeFault"])

    return fallback[:120]


def _selected_manual_citation(response: dict) -> dict | None:
    for evidence in response.get("evidence") or []:
        if evidence.get("source") == "manual":
            return {
                key: evidence[key]
                for key in ("title", "section", "page", "sourceUri", "chunkId", "manualVersion")
                if key in evidence
            }
    return None


def _last_telemetry_snapshot(response: dict) -> dict | None:
    for evidence in response.get("evidence") or []:
        if evidence.get("source") == "telemetry":
            return {
                "title": evidence.get("title"),
                "excerpt": evidence.get("excerpt"),
                "confidence": evidence.get("confidence"),
            }
    return None


def _escalation_state(*, response: dict, steps: list[dict]) -> str:
    if any(step.get("requiresTechnician") or step.get("priority") == "escalate" for step in steps):
        return "recommended"

    if any(
        action.get("priority") == "escalate" for action in response.get("recommendedActions") or []
    ):
        return "recommended"

    return "none"


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _normalized_follow_up(message: str) -> str:
    return re.sub(r"\s+", " ", message.strip().lower().strip(" .!?"))


def _session_http_error(error: SessionError) -> HTTPException:
    headers = {"Retry-After": "2"} if isinstance(error, IdempotencyInProgress) else None
    return HTTPException(
        status_code=getattr(error, "status_code", 500),
        detail=str(error),
        headers=headers,
    )
