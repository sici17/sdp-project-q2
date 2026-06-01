import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Literal, NotRequired, TypedDict
from urllib.parse import unquote, urlparse

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from arol_ai.access import ANONYMOUS, AccessContext, AccessDenied, Domain, check_tenancy
from arol_ai.agents.maintenance import assess_maintenance, maintenance_due_requested
from arol_ai.config import get_settings
from arol_ai.data.manifest_repository import ManifestMachineRepository
from arol_ai.data.repository import MachineContextRepository
from arol_ai.domain.models import (
    Machine,
    MachineContext,
    Manual,
    ManualReference,
    OrderRecord,
    ServiceContract,
    ServiceHistoryRecord,
    TelemetrySnapshot,
)
from arol_ai.graph.edges import classify_intents
from arol_ai.graph.nodes import (
    _requires_grounded_safety_answer,
    action_planner_node,
    answer_node,
    business_agent_node,
    business_data_requested,
    doc_agent_node,
    machine_label,
    supervisor_node,
    telemetry_agent_node,
    troubleshooting_agent_node,
)
from arol_ai.graph.state import ActionItem, DiagnosticStep, Evidence, GraphState
from arol_ai.llm.providers import (
    LLMProvider,
    LLMProviderError,
    build_llm_provider,
    stream_synthesis,
)
from arol_ai.rag.ingestion import _manuals_dir
from arol_ai.tools.base import ToolCallRecord
from arol_ai.tools.registry import ToolRegistry, build_tool_registry

AgentNodeName = Literal["doc-agent", "telemetry-agent", "troubleshooting-agent", "business-agent"]
RouteName = Literal[
    "doc-agent",
    "telemetry-agent",
    "troubleshooting-agent",
    "business-agent",
    "action-planner",
]
AGENT_SEQUENCE: tuple[AgentNodeName, ...] = (
    "doc-agent",
    "telemetry-agent",
    "troubleshooting-agent",
    "business-agent",
)
WORKFLOW_NODES: tuple[str, ...] = (
    "supervisor",
    "doc-agent",
    "telemetry-agent",
    "troubleshooting-agent",
    "business-agent",
    "action-planner",
    "answer",
)
RESPONSE_FIELDS: tuple[str, ...] = (
    "message",
    "agentTrace",
    "intents",
    "routingReason",
    "evidence",
    "toolCalls",
    "diagnosticSteps",
    "recommendedActions",
    "answerConfidence",
    "reviewRequired",
    "reviewReasons",
)


class WorkflowState(TypedDict):
    graph_state: GraphState
    context: MachineContext
    streaming: NotRequired[bool]


class Orchestrator:
    """LangGraph-backed runner for the AROL multi-agent workflow."""

    def __init__(
        self,
        repository: MachineContextRepository | None = None,
        tools: ToolRegistry | None = None,
        llm_provider: LLMProvider | None = None,
        checkpointer: InMemorySaver | None = None,
    ) -> None:
        self.repository = repository or ManifestMachineRepository()
        self.tools = tools or build_tool_registry()
        self.llm_provider = llm_provider or build_llm_provider()
        self.checkpointer = checkpointer or _build_checkpointer()
        self.graph = self._build_graph()

    def complete_chat(self, request: dict) -> dict:
        state = self._run_graph(request)
        return state.to_response()

    def stream_chat(self, request: dict) -> Iterator[str]:
        for event, payload in self.stream_chat_events(request):
            yield _sse_event(event, payload)

    def stream_chat_events(self, request: dict) -> Iterator[tuple[str, dict]]:
        context = self._request_context(request)
        if context.machine is None:
            raise ValueError("Machine not found.")

        workflow_state = self._initial_workflow_state(request, context=context, streaming=True)
        config = {"configurable": {"thread_id": request["sessionId"]}}
        final_state = workflow_state["graph_state"]
        trace_emitted = False

        for update in self.graph.stream(workflow_state, config, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node_name, node_update in update.items():
                if not isinstance(node_update, dict):
                    continue
                graph_state = node_update.get("graph_state")
                if not isinstance(graph_state, GraphState):
                    continue
                final_state = graph_state
                if node_name == "supervisor":
                    yield (
                        "trace",
                        {
                            "agentTrace": final_state.agent_trace,
                            "intents": final_state.intents,
                            "routingReason": final_state.routing_reason,
                        },
                    )
                    trace_emitted = True
                yield (
                    "progress",
                    {
                        "stage": node_name,
                        "status": "completed",
                        "agentTrace": final_state.agent_trace,
                        "toolCallCount": len(final_state.tool_calls),
                    },
                )

        if not trace_emitted:
            yield (
                "trace",
                {
                    "agentTrace": final_state.agent_trace,
                    "intents": final_state.intents,
                    "routingReason": final_state.routing_reason,
                },
            )

        if final_state.answer_context is not None and _requires_grounded_safety_answer(final_state):
            draft = str(final_state.answer_context.get("draft") or "")
            final_state.answer = draft
            yield (
                "progress",
                {
                    "stage": "grounded-safety-answer",
                    "status": "completed",
                    "provider": "deterministic",
                },
            )
            if draft:
                yield ("token", {"delta": draft})
        elif final_state.answer_context is not None:
            yield (
                "progress",
                {
                    "stage": "provider-synthesis",
                    "status": "started",
                    "provider": self.llm_provider.name,
                },
            )
            deltas: list[str] = []
            draft = str(final_state.answer_context.get("draft") or "")
            try:
                for delta in stream_synthesis(self.llm_provider, final_state.answer_context):
                    if not delta:
                        continue
                    deltas.append(delta)
                    yield ("token", {"delta": delta})
            except LLMProviderError:
                # The grounded draft is already composed, so a rewrite that fails
                # costs the answer's wording, not the answer. Once tokens have
                # been sent they cannot be retracted, so a mid-stream failure
                # keeps what was said and appends the rest of the draft.
                if not deltas and draft:
                    deltas.append(draft)
                    yield ("token", {"delta": draft})
                elif draft:
                    remainder = "\n\n" + draft
                    deltas.append(remainder)
                    yield ("token", {"delta": remainder})
            final_state.answer = "".join(deltas)
            yield (
                "progress",
                {
                    "stage": "provider-synthesis",
                    "status": "completed",
                    "provider": self.llm_provider.name,
                },
            )
        elif final_state.answer:
            yield ("token", {"delta": final_state.answer})

        yield ("done", final_state.to_response())

    def _run_graph(self, request: dict) -> GraphState:
        context = self._request_context(request)
        if context.machine is None:
            raise ValueError("Machine not found.")

        initial_state = self._initial_workflow_state(request, context=context, streaming=False)
        config = {"configurable": {"thread_id": request["sessionId"]}}
        final_state = self.graph.invoke(initial_state, config)

        return final_state["graph_state"]

    def _request_context(self, request: dict) -> MachineContext:
        intents = classify_intents(request["message"])
        access: AccessContext = request.get("access") or ANONYMOUS
        # Loaded only if this identity may read it. Fetching a domain and then
        # declining to mention it still puts the data in the answer composer's
        # hands, where one branch that reads it straight from the context is
        # enough to leak it past every check the agents made.
        requested_business = business_data_requested(request["message"])
        resources = {
            resource
            for resource, requested in requested_business.items()
            if requested
            and access.can_see(
                Domain.OPERATIONAL if resource == "service_history" else Domain.COMMERCIAL
            )
        }
        include_business = "business" in intents and bool(resources)
        include_telemetry = bool(
            {"telemetry", "troubleshooting"}.intersection(intents)
        ) and access.can_see(Domain.OPERATIONAL)
        if isinstance(self.repository, ManifestMachineRepository):
            identity = self.repository.get_machine_context(
                request["machineId"],
                include_business=False,
                include_telemetry=False,
            )
            if identity.machine is None or not access.owns(identity.machine.company_id):
                return identity
            context = self.repository.get_machine_context(
                request["machineId"],
                include_business=include_business,
                include_telemetry=include_telemetry,
                business_resources=resources,
            )
        else:
            context = self.repository.get_machine_context(request["machineId"])
        if not access.can_see(Domain.OPERATIONAL):
            context = replace(context, telemetry=None, service_history=[])
            if context.contract:
                context = replace(
                    context,
                    contract=replace(
                        context.contract,
                        open_ticket_count=None,
                        ticket_count=None,
                        last_scheduled_maintenance=None,
                    ),
                )
        if not access.can_see(Domain.COMMERCIAL):
            context = replace(context, contract=None, orders=[], quotes=[])
        return context

    @staticmethod
    def _initial_workflow_state(
        request: dict,
        *,
        context: MachineContext,
        streaming: bool,
    ) -> WorkflowState:
        graph_state = GraphState(
            session_id=request["sessionId"],
            machine_id=request["machineId"],
            user_message=request["message"],
            messages=request.get("messages", []),
            # Defaults to the anonymous context, which can see nothing.
            # A caller that forgets to pass an identity gets refusals, not
            # someone else's data.
            access=request.get("access") or ANONYMOUS,
            # Front matter differs per manual across the corpus - 7, 5, 11, 0 -
            # so this is read per machine rather than assumed.
            printed_page_offset=getattr(context.manual, "printed_page_offset", 0) or 0,
        )
        # The tenant boundary is checked here as well as at the HTTP edge. The
        # gateway is the only caller today, but a check that lives solely in the
        # caller is a check the graph does not have: run the orchestrator
        # directly and another company's machine would answer.
        denial = check_tenancy(
            graph_state.access,
            getattr(context.machine, "company_id", None),
            machine_id=request["machineId"],
        )
        if denial is not None:
            graph_state.deny(denial)

        return {
            "graph_state": graph_state,
            "context": context,
            "streaming": streaming,
        }

    def _build_graph(self):
        workflow = StateGraph(WorkflowState)
        workflow.add_node("supervisor", self._supervisor)
        workflow.add_node("doc-agent", self._doc_agent)
        workflow.add_node("telemetry-agent", self._telemetry_agent)
        workflow.add_node("troubleshooting-agent", self._troubleshooting_agent)
        workflow.add_node("business-agent", self._business_agent)
        workflow.add_node("action-planner", self._action_planner)
        workflow.add_node("answer", self._answer)

        workflow.add_edge(START, "supervisor")
        workflow.add_conditional_edges("supervisor", _route_from_supervisor, _route_map())
        workflow.add_conditional_edges("doc-agent", _route_after_doc_agent, _route_map())
        workflow.add_conditional_edges(
            "telemetry-agent", _route_after_telemetry_agent, _route_map()
        )
        workflow.add_conditional_edges(
            "troubleshooting-agent",
            _route_after_troubleshooting_agent,
            _route_map(),
        )
        workflow.add_edge("business-agent", "action-planner")
        workflow.add_edge("action-planner", "answer")
        workflow.add_edge("answer", END)

        return workflow.compile(checkpointer=self.checkpointer)

    def _supervisor(self, state: WorkflowState) -> dict:
        return {"graph_state": supervisor_node(state["graph_state"], self.llm_provider)}

    def _doc_agent(self, state: WorkflowState) -> dict:
        graph_state = doc_agent_node(
            state["graph_state"],
            state["context"].manual,
            state["context"].telemetry,
            self.tools,
        )
        return {"graph_state": graph_state}

    def _telemetry_agent(self, state: WorkflowState) -> dict:
        graph_state = telemetry_agent_node(
            state["graph_state"],
            state["context"].telemetry,
            self.tools,
        )
        return {"graph_state": graph_state}

    def _troubleshooting_agent(self, state: WorkflowState) -> dict:
        graph_state = troubleshooting_agent_node(
            state["graph_state"],
            state["context"].telemetry,
            self.tools,
        )
        return {"graph_state": graph_state}

    def _business_agent(self, state: WorkflowState) -> dict:
        graph_state = business_agent_node(
            state["graph_state"],
            state["context"].contract,
            state["context"].orders,
            state["context"].service_history,
            state["context"].business_errors,
            self.tools,
            quotes=state["context"].quotes,
        )
        context = state["context"]
        if (
            maintenance_due_requested(graph_state.operator_context)
            and graph_state.access.can_see(Domain.OPERATIONAL)
            and not graph_state.safety_blocked
            and not graph_state.tenancy_denied
            and context.manual
        ):
            try:
                client = self.tools.alarm_lookup.mcp_client
                rows = (
                    client.call_tool(
                        "telemetry.history",
                        {
                            "machineId": graph_state.machine_id,
                            "limit": 1440,
                        },
                    )
                    if client
                    else []
                )
                directory = _manuals_dir(get_settings()).resolve()
                name = Path(unquote(urlparse(context.manual.url).path)).name
                path = (directory / name).resolve()
                if path.parent != directory:
                    raise ValueError("Invalid manual path")
                bullets, evidence = assess_maintenance(
                    path, context.manual.url, rows, context.service_history
                )
                graph_state.maintenance_assessment = bullets
                graph_state.evidence.extend(evidence)
                graph_state.tool_calls.append(
                    ToolCallRecord.create(
                        name="maintenance.assess_due",
                        agent="business-agent",
                        status="ok",
                        input_summary="Machine manual intervals and hourly telemetry",
                        output_summary=f"Compared {len(evidence)} intervals with {len(rows)} hourly readings.",
                    )
                )
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                graph_state.maintenance_assessment = [
                    "Maintenance due status could not be calculated from the available manual and "
                    "telemetry. Please retry or consult the task log."
                ]
                graph_state.tool_calls.append(
                    ToolCallRecord.create(
                        name="maintenance.assess_due",
                        agent="business-agent",
                        status="error",
                        input_summary="Machine manual intervals and hourly telemetry",
                        output_summary=str(exc),
                    )
                )
        return {"graph_state": graph_state}

    def _action_planner(self, state: WorkflowState) -> dict:
        graph_state = action_planner_node(
            state["graph_state"],
            state["context"].telemetry,
            state["context"].contract,
        )
        return {"graph_state": graph_state}

    def _answer(self, state: WorkflowState) -> dict:
        graph_state = answer_node(
            state["graph_state"],
            _context_machine_label(state["context"]),
            state["context"].telemetry,
            state["context"].contract,
            None if state.get("streaming") else self.llm_provider,
            orders=state["context"].orders,
            service_history=state["context"].service_history,
            quotes=state["context"].quotes,
        )
        return {"graph_state": graph_state}


def response_to_sse_events(response: dict) -> Iterator[str]:
    yield _sse_event(
        "trace",
        {
            "agentTrace": response["agentTrace"],
            "intents": response.get("intents", []),
            "routingReason": response.get("routingReason", ""),
        },
    )

    content = response["message"]["content"]
    if content:
        yield _sse_event("token", {"delta": content})

    yield _sse_event("done", response)


def complete_chat(request: dict) -> dict:
    return _DEFAULT_ORCHESTRATOR.complete_chat(request)


def stream_chat(request: dict) -> Iterator[str]:
    return _DEFAULT_ORCHESTRATOR.stream_chat(request)


def stream_chat_events(request: dict) -> Iterator[tuple[str, dict]]:
    return _DEFAULT_ORCHESTRATOR.stream_chat_events(request)


def sse_event(event: str, payload: dict) -> str:
    return _sse_event(event, payload)


def graph_definition(*, session_persistence: str = "in-memory") -> dict:
    return {
        "engine": "langgraph.StateGraph",
        "nodes": list(WORKFLOW_NODES),
        "edges": [
            {"from": "START", "to": "supervisor", "type": "direct"},
            {
                "from": "supervisor",
                "to": [
                    "doc-agent",
                    "telemetry-agent",
                    "troubleshooting-agent",
                    "business-agent",
                    "action-planner",
                ],
                "type": "conditional",
            },
            {
                "from": "doc-agent",
                "to": [
                    "telemetry-agent",
                    "troubleshooting-agent",
                    "business-agent",
                    "action-planner",
                ],
                "type": "conditional",
            },
            {
                "from": "telemetry-agent",
                "to": ["troubleshooting-agent", "business-agent", "action-planner"],
                "type": "conditional",
            },
            {
                "from": "troubleshooting-agent",
                "to": ["business-agent", "action-planner"],
                "type": "conditional",
            },
            {"from": "business-agent", "to": "action-planner", "type": "direct"},
            {"from": "action-planner", "to": "answer", "type": "direct"},
            {"from": "answer", "to": "END", "type": "direct"},
        ],
        "agentSequence": list(AGENT_SEQUENCE),
        "checkpointing": {
            "type": "in-memory",
            "threadIdField": "sessionId",
        },
        "sessionPersistence": {
            "type": session_persistence,
            "ownerField": "ownerSubject",
            "ttlField": "expiresAt",
        },
        "responseFields": list(RESPONSE_FIELDS),
    }


def _route_map() -> dict[RouteName, str]:
    return {
        "doc-agent": "doc-agent",
        "telemetry-agent": "telemetry-agent",
        "troubleshooting-agent": "troubleshooting-agent",
        "business-agent": "business-agent",
        "action-planner": "action-planner",
    }


def _route_from_supervisor(state: WorkflowState) -> RouteName:
    graph_state = state["graph_state"]
    if graph_state.safety_blocked:
        return "action-planner"

    return _next_route(graph_state, after=None)


def _route_after_doc_agent(state: WorkflowState) -> RouteName:
    return _next_route(state["graph_state"], after="doc-agent")


def _route_after_telemetry_agent(state: WorkflowState) -> RouteName:
    return _next_route(state["graph_state"], after="telemetry-agent")


def _route_after_troubleshooting_agent(state: WorkflowState) -> RouteName:
    return _next_route(state["graph_state"], after="troubleshooting-agent")


def _next_route(graph_state: GraphState, after: AgentNodeName | None) -> RouteName:
    start_index = 0 if after is None else AGENT_SEQUENCE.index(after) + 1

    for agent in AGENT_SEQUENCE[start_index:]:
        if agent in graph_state.agent_trace:
            return agent

    return "action-planner"


def _build_checkpointer() -> InMemorySaver:
    serializer = JsonPlusSerializer(
        allowed_msgpack_modules=(
            # The access model travels in the checkpointed state, so its types
            # have to be on this allowlist or a resumed turn silently loses the
            # identity and its recorded refusals.
            AccessContext,
            AccessDenied,
            ActionItem,
            DiagnosticStep,
            Domain,
            Evidence,
            GraphState,
            Machine,
            MachineContext,
            Manual,
            ManualReference,
            OrderRecord,
            ServiceContract,
            ServiceHistoryRecord,
            TelemetrySnapshot,
            ToolCallRecord,
        )
    )
    return InMemorySaver(serde=serializer)


def _context_machine_label(context: MachineContext) -> str:
    return machine_label(context.machine)


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


_DEFAULT_ORCHESTRATOR = Orchestrator()
