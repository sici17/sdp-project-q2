import re

from arol_ai.access import Domain, check_domain
from arol_ai.agents.action_planner import plan_actions
from arol_ai.alarm_codes import contains_alarm_code, extract_alarm_code
from arol_ai.domain.models import (
    Machine,
    Manual,
    OrderRecord,
    QuoteRecord,
    ServiceContract,
    ServiceHistoryRecord,
    TelemetrySnapshot,
    describe_reading,
)
from arol_ai.evaluators.safety import (
    build_guardrail_response,
    classify_blocked_request,
    requires_safety_grounding,
)
from arol_ai.graph.answer_composer import StructuredAnswerComposer
from arol_ai.graph.edges import agents_for_intents, classify_intents, routing_reason
from arol_ai.graph.evidence_selection import (
    manual_retrieval_query as _manual_retrieval_query,
)
from arol_ai.graph.evidence_selection import (
    select_manual_evidence as _select_manual_evidence,
)
from arol_ai.graph.llm_router import classify_with_llm
from arol_ai.graph.state import Evidence, GraphState
from arol_ai.llm.providers import LLMProvider, LLMProviderError
from arol_ai.rag.text import (
    alarm_code_phrases,
    focus_alarm_passage,
    normalize_for_search,
    search_tokens,
)
from arol_ai.tools.base import ToolCallRecord
from arol_ai.tools.registry import ToolRegistry

ACTION_VERBS = (
    "access",
    "check",
    "clear",
    "close",
    "confirm",
    "insert",
    "modify",
    "press",
    "remove",
    "rotate",
    "select",
    "set",
    "unscrew",
    "verify",
)
SAFETY_QUERY_TERMS = {"door", "emergency", "guard", "interlock", "safety", "stop", "voltage"}


def supervisor_node(state: GraphState, llm_provider: LLMProvider | None = None) -> GraphState:
    if state.tenancy_denied:
        # Nothing downstream should run: no agent, no tool, no retrieval. The
        # refusal is the whole answer.
        state.agent_trace = ["supervisor"]
        state.intents = ["access-denied"]
        state.routing_reason = (
            "The machine belongs to another company's fleet. No specialized tools were called."
        )
        return state

    guardrail_reason = classify_blocked_request(state.operator_context)
    if guardrail_reason:
        state.safety_blocked = True
        state.guardrail_reason = guardrail_reason
        state.agent_trace = ["supervisor"]
        state.intents = [guardrail_reason]
        state.routing_reason = (
            f"{guardrail_reason} request detected. No specialized tools were called."
        )
        return state

    state.safety_sensitive = requires_safety_grounding(state.operator_context)
    state.intents = classify_intents(state.operator_context)

    # Keywords could not place the question. Before refusing an operator who may
    # simply be writing in Spanish or typing with gloves on, ask the model. This
    # is the only path it routes, so nothing that already works pays for it, and
    # an unusable answer leaves the refusal exactly as it was.
    if state.intents == ["clarification"]:
        rescued = classify_with_llm(state.operator_context, llm_provider)
        if rescued:
            state.intents = rescued
            state.routed_by_model = True

    state.agent_trace = agents_for_intents(state.intents)
    state.routing_reason = routing_reason(state.intents, state.agent_trace)
    if state.routed_by_model:
        state.routing_reason += (
            " Keyword routing did not recognise the question, so the intents were"
            " established by the model."
        )
    if state.safety_sensitive:
        state.routing_reason += (
            " The question bears on restarting a stopped machine, so live state was"
            " required and the answer was composed from evidence rather than rewritten."
        )
    return state


def doc_agent_node(
    state: GraphState,
    manual: Manual | None,
    telemetry: TelemetrySnapshot | None,
    tools: ToolRegistry,
) -> GraphState:
    if state.safety_blocked or "doc-agent" not in state.agent_trace:
        return state

    query = _manual_retrieval_query(state, telemetry)
    grounding_query = state.operator_context
    if (
        extract_alarm_code(grounding_query)
        and not alarm_code_phrases(grounding_query)
        and state.access.can_see(Domain.OPERATIONAL)
    ):
        lookup = tools.alarm_lookup.run(machine_id=state.machine_id, query=grounding_query)
        if lookup.evidence:
            query = f"{query}\nRecorded condition: {lookup.evidence.excerpt}"
            grounding_query = query
    result = tools.manual_search.run(
        machine_id=state.machine_id,
        manual=manual,
        query=query,
        # The retrieval query may carry the machine's recorded alarm, which the
        # operator never typed. Passing the original message keeps the
        # exact-code rejection tied to a code the operator actually asked
        # about; otherwise "why does this keep alarming" inherits the current
        # code and every manual passage is rejected for not printing it.
        operator_query=grounding_query,
    )
    state.evidence.extend(result.evidence)
    state.tool_calls.append(result.tool_call)
    return state


def telemetry_agent_node(
    state: GraphState,
    telemetry: TelemetrySnapshot | None,
    tools: ToolRegistry,
) -> GraphState:
    if state.safety_blocked or "telemetry-agent" not in state.agent_trace:
        return state

    # Checked before the tool runs, not after: a tool that reads first and
    # filters later has already produced the data it was meant to withhold.
    denial = check_domain(state.access, Domain.OPERATIONAL, machine_id=state.machine_id)
    if denial is not None:
        state.deny(denial)
        return state

    requested_alarm_code = extract_alarm_code(state.operator_context)
    if requested_alarm_code:
        result = tools.alarm_lookup.run(
            machine_id=state.machine_id,
            query=state.operator_context,
        )
        if result.evidence is not None:
            state.evidence.append(result.evidence)
        state.tool_calls.append(result.tool_call)
        return state

    result = tools.telemetry_snapshot.run(telemetry=telemetry)
    if result.evidence is not None:
        state.evidence.append(result.evidence)
    state.tool_calls.append(result.tool_call)
    return state


def troubleshooting_agent_node(
    state: GraphState,
    telemetry: TelemetrySnapshot | None,
    tools: ToolRegistry,
) -> GraphState:
    if state.safety_blocked or "troubleshooting-agent" not in state.agent_trace:
        return state

    # Troubleshooting spans two domains: manual guidance is common to everyone
    # in the company, while telemetry-driven diagnosis is operational. Rather
    # than refuse the whole request, withhold the telemetry and still give the
    # manual-based steps, which is the useful half and the one that is allowed.
    denial = check_domain(state.access, Domain.OPERATIONAL, machine_id=state.machine_id)
    if denial is not None:
        state.deny(denial)
        telemetry = None

    requested_alarm_code = extract_alarm_code(state.operator_context)
    if (
        requested_alarm_code
        and telemetry is not None
        and not contains_alarm_code(telemetry.active_alarm, requested_alarm_code)
    ):
        # The latest snapshot may contain a different alarm. Mixing it into a
        # code-specific question would produce a plausible but unrelated path.
        telemetry = None

    result = tools.troubleshooting.run(
        query=_manual_retrieval_query(state, telemetry),
        telemetry=telemetry,
        evidence=state.evidence,
    )
    state.diagnostic_steps = result.diagnostic_steps
    state.tool_calls.append(result.tool_call)
    return state


def business_agent_node(
    state: GraphState,
    contract: ServiceContract | None,
    orders: list[OrderRecord],
    service_history: list[ServiceHistoryRecord],
    business_errors: dict[str, str],
    tools: ToolRegistry,
    quotes: list[QuoteRecord] | None = None,
) -> GraphState:
    if state.safety_blocked or "business-agent" not in state.agent_trace:
        return state

    requested = business_data_requested(state.operator_context)
    for resource, needed in list(requested.items()):
        if not needed:
            continue
        domain = Domain.OPERATIONAL if resource == "service_history" else Domain.COMMERCIAL
        denial = check_domain(state.access, domain, machine_id=state.machine_id)
        if denial is not None:
            if denial not in state.access_denials:
                state.deny(denial)
            requested[resource] = False
    if requested["quotes"]:
        result = tools.quote_history.run(
            quotes=quotes or [],
            dependency_error=business_errors.get("quotes"),
        )
        if result.evidence is not None:
            state.evidence.append(result.evidence)
        state.tool_calls.append(result.tool_call)
    if requested["entitlement"]:
        result = tools.service_entitlement.run(
            contract=contract,
            dependency_error=business_errors.get("entitlement"),
        )
        if result.evidence is not None:
            state.evidence.append(result.evidence)
        state.tool_calls.append(result.tool_call)
    if requested["orders"]:
        result = tools.order_history.run(
            orders=orders,
            dependency_error=business_errors.get("orders"),
        )
        if result.evidence is not None:
            state.evidence.append(result.evidence)
        state.tool_calls.append(result.tool_call)
    if requested["service_history"]:
        result = tools.service_history.run(
            records=service_history,
            dependency_error=business_errors.get("service_history"),
        )
        if result.evidence is not None:
            state.evidence.append(result.evidence)
        state.tool_calls.append(result.tool_call)
    return state


def action_planner_node(
    state: GraphState,
    telemetry: TelemetrySnapshot | None,
    contract: ServiceContract | None,
) -> GraphState:
    state.recommended_actions = plan_actions(state, telemetry, contract)
    return state


def answer_node(
    state: GraphState,
    machine_label: str,
    telemetry: TelemetrySnapshot | None,
    contract: ServiceContract | None,
    llm_provider: LLMProvider | None = None,
    *,
    orders: list[OrderRecord] | None = None,
    service_history: list[ServiceHistoryRecord] | None = None,
    quotes: list[QuoteRecord] | None = None,
) -> GraphState:
    orders = orders or []
    service_history = service_history or []
    quotes = quotes or []
    # Every agent has run by now, so this is the one place that sees all the
    # evidence and all the steps before a page number is written down.
    state.resolve_printed_pages()
    if state.safety_blocked:
        state.answer = build_guardrail_response(machine_label, state.guardrail_reason)
        return state

    # When everything the turn asked for was refused, the refusal is the answer.
    # Saying it plainly is the point: an operator must never be left to read a
    # thinner reply as "there is nothing to report".
    if state.access_denials and not state.evidence and not state.diagnostic_steps:
        state.answer = _access_denial_answer(state, machine_label)
        return state

    if "clarification" in state.intents:
        state.answer = (
            "I couldn't identify a machine-related request in that question. "
            "Service Assist helps with this machine's manual, alarms, operating data, "
            "maintenance, and commercial records. Please describe the machine issue "
            "or specify the information you need."
        )
        return state

    if "conversation" in state.intents:
        state.answer = (
            "I'm here and ready to help with this machine. Ask me about alarms, procedures, "
            "telemetry, warranty, or escalation details and I will use the available machine context."
        )
        return state

    active_agents = state.agent_trace[1:] or ["supervisor"]
    composer = StructuredAnswerComposer(machine_label=machine_label, route=active_agents)
    requested_alarm_code = extract_alarm_code(state.operator_context)
    alarm_evidence = next(
        (
            item
            for item in state.evidence
            if item.source == "telemetry"
            and contains_alarm_code(
                " ".join((item.title, item.excerpt, *item.alarm_codes)),
                requested_alarm_code or "",
            )
        ),
        None,
    )

    if alarm_evidence is not None:
        composer.add_bullets("Alarm code", [alarm_evidence.excerpt])
    elif (
        requested_alarm_code and "telemetry-agent" in state.agent_trace and not state.access_denials
    ):
        composer.add_bullets(
            "Alarm code",
            [f"No {requested_alarm_code} record was found in this machine's telemetry history."],
        )

    if "telemetry-agent" in state.agent_trace and telemetry and not requested_alarm_code:
        telemetry_sentence = (
            f"Current telemetry is {telemetry.health}: {describe_reading(telemetry)}"
        )

        if telemetry.has_active_alarm:
            telemetry_sentence += f", and active alarm {telemetry.active_alarm}"

        composer.add_bullets("Telemetry", [f"{telemetry_sentence}."])

    reset_gate = next(
        (step for step in state.diagnostic_steps if step.step_id == "preserve-safety-protections"),
        None,
    )
    if reset_gate is not None:
        composer.add_bullets("Before reset", [reset_gate.detail])

    manual_query = _manual_retrieval_query(state, telemetry)
    manual_evidence = _select_manual_evidence(state.evidence, manual_query)
    manual_guidance = (
        _manual_guidance_sentence(manual_evidence, manual_query)
        if manual_evidence and not state.maintenance_assessment
        else ""
    )
    if manual_guidance:
        state.evidence = _prioritize_selected_evidence(state.evidence, manual_evidence)
        composer.add_raw(manual_guidance)
    elif "doc-agent" in state.agent_trace and not state.maintenance_assessment:
        composer.add_bullets(
            "Manual summary",
            ["I could not find a cited manual passage for that topic in the current machine data."],
        )

    requested_business_data = business_data_requested(state.operator_context)
    if state.maintenance_assessment:
        composer.add_bullets("Maintenance due", state.maintenance_assessment)
    wants_entitlement = _business_section(
        state, requested_business_data, "entitlement", Domain.COMMERCIAL
    )
    if wants_entitlement and contract:
        composer.add_bullets("Service standing", _service_standing_bullets(contract))
    elif wants_entitlement:
        composer.add_bullets(
            "Service standing",
            ["No service record is available for this machine."],
        )

    if _business_section(state, requested_business_data, "quotes", Domain.COMMERCIAL):
        composer.add_bullets(
            "Quotations",
            [_quote_sentence(quote) for quote in _recent_quotes(quotes)]
            or ["No quotations were issued to this company."],
        )

    if _business_section(state, requested_business_data, "orders", Domain.COMMERCIAL):
        composer.add_bullets(
            "Orders",
            [_order_sentence(item) for item in _recent_orders(orders)]
            or ["No orders were found for this machine."],
        )

    if (
        # Maintenance history is operational data, not commercial - the one
        # guard of the four that differs, now visible as an argument.
        _business_section(state, requested_business_data, "service_history", Domain.OPERATIONAL)
        and not state.maintenance_assessment
    ):
        open_count = sum(
            item.status in {"Open", "In progress", "Waiting for parts"} for item in service_history
        )
        # An operator asking what is open is asking about outstanding work.
        # Listing closed tickets alongside invited the answer to describe a
        # resolved ticket as though it were still running.
        wants_open_only = _asks_for_open_work(state.operator_context)
        listed = (
            [item for item in service_history if item.is_open]
            if wants_open_only
            else service_history
        )
        composer.add_bullets(
            "Open maintenance tickets" if wants_open_only else "Maintenance history",
            [f"Maintenance tickets: {open_count} open of {len(service_history)} recorded"]
            + (
                [_ticket_sentence(item) for item in _recent_tickets(listed)]
                or ["No maintenance tickets are currently open for this machine."]
            ),
        )

    if requested_alarm_code and alarm_evidence is not None and not manual_evidence:
        composer.add_bullets(
            "Next step",
            [
                "The telemetry record identifies the alarm condition but does not provide a repair "
                "procedure. Use a cited machine-manual procedure or escalate to a qualified AROL "
                "technician before intervening."
            ],
        )
    elif "telemetry-agent" in state.agent_trace and telemetry and telemetry.has_active_alarm:
        composer.add_bullets(
            "Next step",
            [
                "If the alarm remains after the operator checks, escalate to a qualified AROL technician "
                "with the alarm code and latest telemetry snapshot."
            ],
        )
    elif "telemetry-agent" in state.agent_trace:
        composer.add_bullets(
            "Next step",
            [
                "If the condition changes or an alarm appears, capture the code and latest telemetry before escalating."
            ],
        )

    if state.diagnostic_steps:
        composer.add_raw(_diagnostic_steps_sentence(state))

    # A partial refusal still has to be stated. Answering the half that was
    # allowed and staying silent about the rest would read as a complete answer.
    if state.access_denials:
        composer.add_bullets(
            "Not available to you",
            [denial.message for denial in state.access_denials],
        )

    draft = composer.render()
    state.answer = _synthesize_with_provider(
        state=state,
        machine_label=machine_label,
        telemetry=telemetry,
        contract=contract,
        orders=orders,
        service_history=service_history,
        quotes=quotes,
        draft=draft,
        structured_answer=composer.to_dict(),
        llm_provider=llm_provider,
    )
    return state


def _business_section(state: GraphState, requested: dict[str, bool], key: str, domain: str) -> bool:
    """Whether one business section belongs in this answer.

    The three clauses were repeated at every call site, twice on lines long
    enough that the differences had to be found by eye - and one of the four
    guards requires a different domain from the other three. In access-control
    code that is the wrong thing to leave as a line to diff.
    """
    return "business-agent" in state.agent_trace and requested[key] and state.access.can_see(domain)


def business_data_requested(query: str) -> dict[str, bool]:
    normalized = normalize_for_search(query)
    order_requested = "order" in normalized or "spare part" in normalized
    service_history_requested = "maintenance" in normalized or any(
        phrase in normalized
        for phrase in (
            "service history",
            "service due",
            "next service",
            "maintenance history",
            "last service",
            "previous service",
            "service record",
            "maintenance record",
            "work performed",
        )
    )
    entitlement_requested = any(
        term in normalized
        for term in (
            "contract",
            "warranty",
            "sla",
            "coverage",
            "entitlement",
            # The delivery date and the acquisition value live on the service
            # standing record, so "when was it delivered and what did it cost"
            # has to reach it as well as the quotations.
            "deliver",
            "delivered",
            "delivery",
            "installed",
            "commissioned",
            "purchase",
            "bought",
            "acquisition",
        )
    )
    quote_requested = any(
        term in normalized
        for term in (
            "quote",
            "quotes",
            "quotation",
            "quotations",
            "price",
            "prices",
            "pricing",
            "cost",
            "revision",
            "discount",
            "offer",
        )
    )
    if not any(
        (order_requested, service_history_requested, entitlement_requested, quote_requested)
    ):
        entitlement_requested = True
    return {
        "entitlement": entitlement_requested,
        "quotes": quote_requested,
        "orders": order_requested,
        "service_history": service_history_requested,
    }


def _is_procedure_like(text: str) -> bool:
    return any(
        cue in text
        for cue in (
            "operate as follows",
            "proceed as follows",
            "listed below",
            "it is necessary",
            "press",
            "remove",
            "unscrew",
        )
    )


def _is_figure_only_candidate(title: str, excerpt: str) -> bool:
    normalized_title = normalize_for_search(title)
    if normalized_title.startswith("fig"):
        return True

    tableish = excerpt.count("|") >= 2 or "spring kgf" in excerpt
    has_steps = sum(1 for verb in ACTION_VERBS if verb in excerpt) >= 2
    return tableish and not has_steps


def _prioritize_selected_evidence(
    evidence: list[Evidence],
    selected: Evidence,
) -> list[Evidence]:
    return [selected, *[item for item in evidence if item is not selected]]


def _manual_guidance_sentence(evidence: Evidence | str, query: str = "") -> str:
    excerpt = evidence.excerpt if isinstance(evidence, Evidence) else evidence
    excerpt = focus_alarm_passage(excerpt, query)
    title = _manual_evidence_title(evidence)
    lines = _clean_manual_lines(excerpt)
    if alarm_code_phrases(query) and lines:
        # Alarm tables are frequently broken at PDF column/newline boundaries.
        # Rejoin the isolated row before sentence splitting so fragments such
        # as "START to re-start the" and "closing machine" remain one remedy.
        lines = [" ".join(lines)]
    specific_guidance = _specific_manual_guidance(lines, title=title, query=query)
    if specific_guidance:
        return _format_manual_guidance(
            summary=specific_guidance,
            checks=_specific_manual_checks(lines, title=title, query=query),
            evidence=evidence,
        )

    procedure_steps = _procedure_steps(lines)
    if procedure_steps:
        return _format_manual_guidance(
            summary="Follow the cited procedure in order and keep the manual page open while you work.",
            checks=procedure_steps,
            evidence=evidence,
        )

    table_guidance = _table_guidance(lines, query)
    if table_guidance and _reads_as_prose(table_guidance):
        return _format_manual_guidance(summary=table_guidance, evidence=evidence)

    guidance = _summarize_manual_lines(lines, title=title, query=query)
    if not _reads_as_prose(guidance):
        # Nothing in the passage survived as readable guidance. The caller's
        # own wording for an empty retrieval is the honest answer here too.
        return ""

    return _format_manual_guidance(summary=guidance, evidence=evidence)


def _format_manual_guidance(
    *,
    summary: str,
    evidence: Evidence | str,
    checks: list[str] | tuple[str, ...] = (),
) -> str:
    lines = ["Manual summary", f"- {summary}"]
    if checks:
        lines.append("What to check")
        lines.extend(f"{index}. {check}" for index, check in enumerate(checks, start=1))

    if isinstance(evidence, Evidence):
        safety_note = _manual_safety_note(evidence)
        if safety_note:
            lines.extend(["Safety", f"- {safety_note}"])

        citation = _citation_label(evidence)
        if evidence.page is not None:
            citation = f"{citation}, page {evidence.printed_page or evidence.page}"
        lines.extend(["Citation", f"- {citation}"])

    return "\n".join(lines)


def _manual_safety_note(evidence: Evidence) -> str:
    if evidence.safety_level == "safety-critical":
        return (
            "Stop before guarded, interlocked, or electrical intervention and escalate to a "
            "qualified technician."
        )

    if evidence.safety_level == "technician":
        return "Treat technician-marked work as qualified maintenance work, not a routine operator check."

    return ""


def _manual_evidence_title(evidence: Evidence | str) -> str:
    if isinstance(evidence, Evidence):
        return evidence.section or evidence.title

    return ""


def _citation_label(evidence: Evidence) -> str:
    title = evidence.section or evidence.title
    if not _is_generic_citation_title(title):
        return title

    text = f"{title}\n{evidence.excerpt}".upper()
    known_sections = (
        "CLOSURE GRIPPER ADJUSTMENTS",
        "MAINTENANCE OF THE CLOSING MACHINE",
        "GENERAL INDICATIONS ON THE SCHEDULED MAINTENANCE PLAN",
        "SAFETY PROCEDURES FOR THE MAINTENANCE",
        "MODIFICATION OF THE CONFIGURATION PARAMETERS",
        "DESCRIPTION OF THE ICONS FUNCTIONS",
        "VERIFICATION OF CORRECT PHASE SEQUENCE",
    )
    for section in known_sections:
        if section in text:
            return section

    return title


def _is_generic_citation_title(title: str) -> bool:
    normalized = normalize_for_search(title)
    return normalized.startswith("the table below") or normalized in {
        "message",
        "symbol",
        "arol",
    }


def _clean_manual_lines(excerpt: str) -> list[str]:
    lines = [
        re.sub(r"^(?:[\u2022\u25ab\-]+|â¢)\s*", "", line.strip())
        for line in excerpt.splitlines()
        if line.strip()
    ]
    return [
        line
        for line in lines
        if not line.lower().startswith("section:")
        and "this manual is property of arol" not in line.lower()
        and not line.isdigit()
        and not (len(line) <= 10 and line.isupper())
    ]


#: A clause the PDF cut at a line break. `_simplify_manual_sentence` ends
#: every candidate with a full stop, which turned "Insert a 4 mm allen screw
#: wrench in the seats of" into a sentence claiming to be complete.
_DANGLING_CLAUSE = re.compile(
    r"\b(?:is|are|was|were|of|the|a|an|in|on|to|for|and|or|with|from|at|by"
    r"|into|onto|as|than|that|which|when|while|between)\.$",
    re.IGNORECASE,
)


def _summarize_manual_lines(lines: list[str], *, title: str, query: str) -> str:
    candidates = [
        _simplify_manual_sentence(sentence)
        for line in lines
        if not _is_manual_noise_line(line)
        for sentence in _split_manual_sentences(line)
    ]
    candidates = [
        sentence for sentence in candidates if sentence and not _DANGLING_CLAUSE.search(sentence)
    ]
    if not candidates:
        topic = _manual_topic(title, lines)
        return f"The cited section covers {topic}; keep the manual page open for the exact limits and figures."

    query_terms = search_tokens(query)
    ranked = sorted(
        candidates,
        key=lambda sentence: _summary_sentence_score(sentence, query_terms),
        reverse=True,
    )
    selected: list[str] = []
    seen: set[str] = set()
    for sentence in ranked:
        normalized = normalize_for_search(sentence)
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(sentence)
        if len(selected) == 3:
            break

    summary = " ".join(selected)
    if len(summary) > 520:
        summary = f"{summary[:517].rstrip()}..."

    return f"{summary[:1].upper()}{summary[1:]}"


def _split_manual_sentences(line: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", line.strip())
    if not normalized:
        return []

    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", normalized)
        if len(sentence.strip()) >= 18
        or any(sentence.strip().lower().startswith(f"{verb} ") for verb in ACTION_VERBS)
    ]


def _simplify_manual_sentence(sentence: str) -> str:
    simplified = re.sub(r"^\d+(?:\.\d+)*\s+", "", sentence.strip())
    simplified = re.sub(r"\bFig\.\s*\d+\b", "", simplified, flags=re.I)
    simplified = re.sub(r"\bSTARTto\b", "START to", simplified, flags=re.I)
    simplified = re.sub(r"\bre-start\b", "restart", simplified, flags=re.I)
    simplified = re.sub(r"\s+", " ", simplified).strip(" .")
    replacements = {
        "It is possible to": "You can",
        "it is possible to": "you can",
        "it is necessary to": "you need to",
        "It is necessary to": "You need to",
        "proceed as follows": "follow the procedure",
        "Proceed as follows": "Follow the procedure",
        "Following the instructions listed below": "Then follow the listed procedure",
        "following the instructions listed below": "then follow the listed procedure",
    }
    for original, replacement in replacements.items():
        simplified = simplified.replace(original, replacement)

    # A clause ending in a colon introduces the list that follows it. Appending
    # a full stop produced "check the following:." in the middle of an answer.
    if simplified.endswith(":"):
        return simplified

    if not simplified.endswith("."):
        simplified = f"{simplified}."

    return simplified


def _summary_sentence_score(sentence: str, query_terms: set[str]) -> int:
    sentence_terms = search_tokens(sentence)
    score = len(query_terms.intersection(sentence_terms))
    if any(verb in sentence.lower() for verb in ACTION_VERBS):
        score += 3
    if sentence.isupper():
        score -= 4
    if "manual is property" in sentence.lower():
        score -= 20

    return score


def _manual_topic(title: str, lines: list[str]) -> str:
    topic = title or next((line for line in lines if line), "the requested topic")
    topic = re.sub(r"^\d+(?:\.\d+)*\s+", "", topic).strip()
    return topic.lower() if topic.isupper() else topic


def _is_manual_noise_line(line: str) -> bool:
    normalized = line.strip()
    lower = normalized.lower()
    if lower.startswith("fig."):
        return True
    if lower in {"symbol", "function", "description"}:
        return True
    if "|" in normalized and len(normalized.split("|")) >= 3:
        return True
    if normalized.isupper() and len(normalized) <= 90:
        return True

    return False


def _specific_manual_guidance(lines: list[str], *, title: str, query: str) -> str:
    """Guidance derived from the cited passage itself.

    This used to hold prose keyed to section titles of one legacy manual. That
    text was written by hand rather than taken from the manual, so against the
    per-machine fleet manuals it attached confident-sounding instructions to
    unrelated pages. Whatever is said here now has to come out of the chunk that
    is being cited.
    """
    table_guidance = _table_guidance(lines, query)
    if table_guidance:
        return table_guidance

    controls_guidance = _controls_guidance(lines)
    if controls_guidance:
        return controls_guidance

    return ""


def _specific_manual_checks(lines: list[str], *, title: str, query: str) -> list[str]:
    """Checks taken from the cited passage.

    Reads the numbered or bulleted steps the manual itself gives, rather than
    reproducing a fixed checklist that may not match this machine.
    """
    return _procedure_steps(lines)


def _procedure_steps(lines: list[str]) -> list[str]:
    steps: list[str] = []
    collecting = False
    for line in lines:
        lower = line.lower()
        if (
            lower.startswith("after ")
            or "operate as follows" in lower
            or "proceed as follows" in lower
            or "proceed as described below" in lower
        ):
            collecting = True
            after_colon = line.split(":", 1)[1].strip() if ":" in line else ""
            if after_colon:
                steps.append(_sentence_case(after_colon))
            continue

        if collecting:
            if steps and line.lower().startswith(("to ", "and ")):
                steps[-1] = f"{steps[-1]} {line}"
                continue
            if line.endswith(":"):
                continue
            if len(line) < 4:
                continue
            if line.lower().startswith("fig."):
                continue
            steps.append(_sentence_case(line))

    return steps[:10]


def _controls_guidance(lines: list[str]) -> str:
    text = " ".join(lines)
    if "CLOSING MACHINE CONTROLS" not in text:
        return ""

    return (
        "The closing-machine controls are reached from the operator-panel main page. "
        "Open the controls page from the main page, select the icon for the operation you need, "
        "and use the return icon to go back to the main page. "
        "Use the cited manual page for the icon mapping before actuating a control."
    )


#: A run of letters long enough to be a word rather than table debris.
_PROSE_WORD = re.compile(r"[A-Za-z]{2,}")


def _reads_as_prose(value: str) -> bool:
    """True when rendered guidance is made of words.

    Extraction hands back table cells as readily as sentences, and the
    renderers below will dutifully format either. That produced summaries
    like "The manual lists - in its fault table. AIM. NOTES." - confident,
    cited, and meaningless. Saying no passage was usable is the better
    answer, and the caller already has that wording.
    """
    text = value or ""
    # A pipe means a table row reached a renderer that formats sentences.
    if "|" in text:
        return False
    return len(_PROSE_WORD.findall(text)) >= 4


def _table_guidance(lines: list[str], query: str = "") -> str:
    """Render a fault-table row as prose.

    Every manual in the fleet documents its alarms as a table, headed MESSAGE /
    DESCRIPTION / RESET MODE or FAULT / CAUSE / REMEDY or PROBLEM / CAUSE /
    SOLUTION. Extraction gives those back as pipe-delimited rows, which are the
    answer to an alarm question but unreadable as they stand. Reading the row
    generically means this works for every manual rather than for the handful of
    conditions someone thought to write out by hand.
    """
    rows = [line for line in lines if line.count("|") >= 2]
    if not rows:
        return ""

    header_terms = {
        "message",
        "description",
        "reset",
        "mode",
        "fault",
        "cause",
        "remedy",
        "problem",
        "solution",
        "n.",
    }
    query_terms = set(normalize_for_search(query).split()) - {"what", "does", "mean", "is"}

    best = None
    best_overlap = -1
    for row in rows:
        cells = [cell.strip(" .") for cell in row.split("|") if cell.strip(" .")]
        if not cells:
            continue
        # A row that is only column headings describes no condition.
        if all(normalize_for_search(cell) in header_terms for cell in cells):
            continue
        # Nor does a row whose cells are all one-word labels. Every real fault
        # row carries a description; "OPERATION | AIM | NOTES" is the heading of
        # a maintenance table and was being reported as a documented fault.
        if not any(_reads_as_prose(cell) for cell in cells):
            continue
        overlap = len(query_terms.intersection(set(normalize_for_search(row).split())))
        if overlap > best_overlap:
            best, best_overlap = cells, overlap

    if not best:
        return ""

    # Drop a leading row number, which carries no meaning outside the table.
    if best[0].isdigit():
        best = best[1:]
    if not best:
        return ""

    condition, *rest = best
    # An empty cell survives extraction as "-", and a header stub as "AIM" or
    # "NOTES". Rendered, those became a sentence claiming the manual documents
    # a condition called "-".
    if not _PROSE_WORD.search(condition):
        return ""

    detail = " ".join(part.rstrip(".") + "." for part in rest if part and _PROSE_WORD.search(part))
    if detail and not _reads_as_prose(detail):
        detail = ""
    if detail:
        return f"The manual lists {condition} in its fault table. {detail}"
    return f"The manual lists {condition} in its fault table."


def _sentence_case(value: str) -> str:
    if not value:
        return value

    return f"{value[:1].upper()}{value[1:]}"


def _service_standing_bullets(contract: ServiceContract) -> list[str]:
    """What the dataset knows about this machine's service relationship.

    There is deliberately no warranty or SLA line: the dataset has neither, and
    a row reading "Warranty: Unavailable" would be read as "not covered".
    """
    bullets = []
    if contract.delivery_date:
        bullets.append(f"Delivered: {contract.delivery_date}")
    if contract.acquisition_summary:
        bullets.append(f"Acquisition value: {contract.acquisition_summary}")
    if contract.open_ticket_count is not None:
        bullets.append(
            f"Maintenance tickets: {contract.open_ticket_count} open of {contract.ticket_count} recorded"
        )
    if contract.last_scheduled_maintenance:
        bullets.append(f"Last scheduled maintenance: {contract.last_scheduled_maintenance}")
    if contract.coverage_note:
        bullets.append(contract.coverage_note)
    return bullets


def _recent_quotes(quotes: list[QuoteRecord]) -> list[QuoteRecord]:
    return sorted(quotes, key=lambda quote: quote.created_at or "", reverse=True)[:5]


def _recent_orders(orders: list[OrderRecord]) -> list[OrderRecord]:
    return sorted(orders, key=lambda order: order.order_date or "", reverse=True)[:5]


def _order_sentence(order: OrderRecord) -> str:
    """One order, including what it actually contained.

    The dataset's order lines carry fulfilment only, so the content comes from
    the approved revision's quote lines. Naming the first of them is what makes
    "how do I order spare parts" answerable from history rather than in the
    abstract.
    """
    parts = [f"{order.order_id}: {order.status or 'state unknown'}"]
    if order.shipment_status and order.shipment_status != order.status:
        parts.append(f"shipment {order.shipment_status}")
    if order.order_date:
        parts.append(f"ordered {order.order_date}")
    if order.expected_delivery_date:
        parts.append(f"expected {order.expected_delivery_date}")
    if order.total_summary:
        parts.append(order.total_summary)
    if order.quote_id:
        parts.append(f"from {order.quote_id}")
    first_line = next(
        (str(line.get("description")) for line in order.lines if line.get("description")),
        None,
    )
    if first_line:
        remainder = len(order.lines) - 1
        parts.append(
            first_line
            + (f" (+{remainder} more line{'s' if remainder != 1 else ''})" if remainder else "")
        )
    return " - ".join(parts)


def _asks_for_open_work(query: str) -> bool:
    """True when the question is about outstanding work, not the whole history."""
    normalized = normalize_for_search(query)
    return any(
        term in normalized
        for term in ("open", "outstanding", "unresolved", "still", "pending", "ongoing")
    )


def _recent_tickets(records: list[ServiceHistoryRecord]) -> list[ServiceHistoryRecord]:
    # Open work first, then the most recent: an operator asking about
    # maintenance cares about what is still outstanding before what is done.
    return sorted(
        records,
        key=lambda record: (record.is_open, record.created_date or ""),
        reverse=True,
    )[:5]


def _ticket_sentence(ticket: ServiceHistoryRecord) -> str:
    parts = [f"{ticket.ticket_id}: {ticket.ticket_type or 'ticket'}"]
    parts.append(f"{ticket.status or 'state unknown'}")
    if ticket.priority:
        parts.append(f"{ticket.priority.lower()} priority")
    if ticket.created_date:
        parts.append(f"raised {ticket.created_date}")
    if ticket.alarm_code:
        parts.append(f"from alarm {ticket.alarm_code}")
    if ticket.owner_role:
        parts.append(f"owned by {ticket.owner_role}")
    return " - ".join(parts)


def _quote_sentence(quote: QuoteRecord) -> str:
    """One quotation, in the terms the dataset actually records.

    The status belongs to the current revision, not to the quote, and the total
    is already net of that revision's discount. Both are easy to misreport in a
    way that reads as a live offer when the offer is closed, so the revision
    number and its state lead the sentence.
    """
    parts = [f"{quote.quote_id}: {quote.description or 'quotation'}"]
    if quote.current_revision_number:
        parts.append(
            f"revision {quote.current_revision_number} of {quote.revision_count}"
            f" ({quote.current_revision_status or 'state unknown'})"
        )
    if quote.current_total is not None and quote.currency:
        total = f"{quote.current_total:,.2f} {quote.currency}"
        if quote.discount_rate:
            total += f" ({quote.discount_rate:.0%} discount applied)"
        parts.append(total)
    if quote.change_summary:
        parts.append(quote.change_summary)
    if quote.expired:
        parts.append(f"past its validity date of {quote.valid_until}")
    elif quote.valid_until:
        parts.append(f"valid until {quote.valid_until}")

    return " - ".join(parts)


def _diagnostic_steps_sentence(state: GraphState) -> str:
    steps = [
        f"{index}. {step.label}"
        for index, step in enumerate(
            (
                step
                for step in state.diagnostic_steps
                if step.step_id not in {"record-active-alarm", "preserve-safety-protections"}
            ),
            start=1,
        )
    ]
    formatted_steps = "\n".join(steps)
    return f"Recommended diagnostic path\n{formatted_steps}"


def _synthesize_with_provider(
    *,
    state: GraphState,
    machine_label: str,
    telemetry: TelemetrySnapshot | None,
    contract: ServiceContract | None,
    orders: list[OrderRecord],
    service_history: list[ServiceHistoryRecord],
    quotes: list[QuoteRecord],
    draft: str,
    structured_answer: dict,
    llm_provider: LLMProvider | None,
) -> str:
    context = {
        "draft": draft,
        "structuredAnswer": structured_answer,
        "machineLabel": machine_label,
        "userMessage": state.user_message,
        "agentTrace": state.agent_trace,
        "intents": state.intents,
        "evidence": [item.to_dict() for item in state.evidence],
        "toolCalls": [item.to_dict() for item in state.tool_calls],
        "diagnosticSteps": [item.to_dict() for item in state.diagnostic_steps],
        "recommendedActions": [item.to_dict() for item in state.recommended_actions],
        "telemetry": telemetry.to_dict() if telemetry else None,
        "contract": contract.to_dict() if contract else None,
        "orders": [item.to_dict() for item in orders],
        "serviceHistory": [item.to_dict() for item in service_history],
        "quotes": [item.to_dict() for item in quotes],
        # Passed explicitly so a synthesizing model is told what was withheld
        # rather than having to notice its absence.
        "accessDenials": [item.to_dict() for item in state.access_denials],
        "messages": state.messages[-8:],
    }
    state.answer_context = context
    if llm_provider is None:
        return draft

    if _requires_grounded_safety_answer(state):
        return draft

    if state.access_denials:
        # A partial refusal is a statement about what this identity may not see,
        # and it has to survive intact. Rewritten, it came back renumbered as
        # step 2 of the diagnostic path - an instruction the operator cannot
        # carry out - and then repeated underneath. The composer already states
        # it once, in its own section.
        return draft

    try:
        return llm_provider.synthesize(context)
    except LLMProviderError as exc:
        # The grounded draft is already in hand. Losing the whole turn because a
        # rewrite timed out would be a worse answer than the one we already
        # have, and on a small local model running on CPU a timeout is an
        # ordinary event rather than an exceptional one.
        state.tool_calls.append(
            ToolCallRecord.create(
                name="llm.synthesize",
                agent="answer",
                status="error",
                input_summary=f"provider {llm_provider.name}",
                output_summary=f"{exc}. Answered from the grounded draft instead.",
            )
        )
        return draft


def _requires_grounded_safety_answer(state: GraphState) -> bool:
    """Safety gates must not be weakened by a best-effort prose rewrite.

    Keyed on the question as well as on the steps. Keying it on the steps alone
    meant a question that produced none got no protection at all, and "can I
    safely restart it?" is exactly that question: the manual agent ran by
    itself, no diagnostic step existed to be safety-critical, and the model was
    left free to turn "check that there are no leaks" into "there are no leaks".
    """
    if state.safety_sensitive:
        return True
    return any(step.safety_level == "safety-critical" for step in state.diagnostic_steps)


def machine_label(machine: Machine | None) -> str:
    if machine is None:
        return "the selected machine"

    return f"{machine.model} ({machine.serial_number})"


def _access_denial_answer(state: GraphState, machine_label: str) -> str:
    """The whole reply when every part of the request was outside the user's scope.

    States what was refused, why, and who can help, so the operator has a next
    step rather than a dead end. It never substitutes data from another company
    and never implies the data does not exist.
    """
    # A machine outside the user's company is described in no terms of ours:
    # naming its model back to them would be a small leak from our records
    # inside the very reply that refuses to read them.
    subject = "that machine" if state.tenancy_denied else machine_label
    lines = [f"I cannot answer that for {subject}."]
    lines.extend(f"- {denial.message}" for denial in state.access_denials)

    if state.tenancy_denied:
        lines.append(
            "If you scanned this machine on your own line, contact AROL support: "
            "the machine may be registered to a different company."
        )
    else:
        lines.append(
            "I can still help with this machine's manual, its configuration and its documentation."
        )

    return "\n".join(lines)
