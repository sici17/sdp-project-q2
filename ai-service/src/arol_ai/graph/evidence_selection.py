"""Which manual passage an answer is built on.

One question produces a citation, a diagnostic checklist and a set of
recommended actions. All three point at a manual passage, and all three used to
pick one for themselves: ``_select_manual_evidence`` was defined three times,
with three signatures, ranking the same candidates by three different scoring
functions.

They did not agree. Asked why a machine was stopped, the answer cited CLOSING
MACHINE USE on printed page 38 while its own checklist pointed at MAINTENANCE OF
THE CLOSING MACHINE on page 87 - two sections of the same manual, forty-nine
pages apart, in one reply.

The intent was already written down. ``manual_retrieval_query`` below carries a
docstring saying the resolved query exists so that "retrieval, answer selection,
and troubleshooting all rank evidence for the same condition". The query was
shared; the ranking was not. This module is the ranking.
"""

import re

from arol_ai.access import Domain, check_domain
from arol_ai.alarm_codes import contains_alarm_code, extract_alarm_code
from arol_ai.domain.models import TelemetrySnapshot
from arol_ai.graph.state import Evidence, GraphState
from arol_ai.rag.scoring import SAFETY_QUERY_TERMS
from arol_ai.rag.text import normalize_for_search, search_tokens

#: Verbs that mark a passage as something to carry out rather than describe.
ACTION_VERBS = (
    "adjust",
    "check",
    "clean",
    "close",
    "connect",
    "disconnect",
    "insert",
    "loosen",
    "lower",
    "lubricate",
    "measure",
    "mount",
    "open",
    "position",
    "press",
    "raise",
    "release",
    "remove",
    "replace",
    "reset",
    "rotate",
    "select",
    "set",
    "unscrew",
    "verify",
)

#: Question vocabulary that makes a procedure or troubleshooting chunk the right
#: kind of passage to answer from.
PROCEDURE_QUERY_TERMS = frozenset(
    {"alarm", "alarms", "check", "error", "fault", "inspect", "procedure", "verify"}
)


def select_manual_evidence(evidence: list[Evidence], query: str) -> Evidence | None:
    """The one manual passage this turn is answered from.

    Every consumer calls this, so the citation, the checklist and the
    recommended actions name the same section by construction rather than by
    three rankings happening to agree.
    """
    manual_evidence = [item for item in evidence if item.source == "manual"]
    if not manual_evidence:
        return None

    return max(manual_evidence, key=lambda item: manual_evidence_score(item, query))


def manual_evidence_score(evidence: Evidence, query: str) -> float:
    """How well a passage answers this question.

    Retrieval decided what is plausible; this decides which of those the
    operator is shown. It reads the title as well as the body, because a
    section named for the question is a better answer than one that merely
    mentions its words, and it prefers a passage written as steps.
    """
    query_terms = search_tokens(query)
    title_text = " ".join(part for part in [evidence.title, evidence.section or ""] if part)
    title_terms = search_tokens(title_text)
    excerpt_terms = search_tokens(evidence.excerpt)
    normalized_query = normalize_for_search(query)
    normalized_excerpt = normalize_for_search(evidence.excerpt)

    score = float(evidence.score or evidence.confidence or 0.0)
    score += 0.08 * len(query_terms.intersection(excerpt_terms))
    score += 0.35 * len(query_terms.intersection(title_terms))
    if normalized_query and normalized_query in normalized_excerpt:
        score += 1.2
    if query_terms and query_terms.issubset(excerpt_terms):
        score += 0.8
    score += 0.22 * len(query_terms.intersection(set(evidence.topics)))

    if evidence.chunk_kind in {"procedure", "troubleshooting"} and query_terms.intersection(
        PROCEDURE_QUERY_TERMS
    ):
        score += 0.55
    if evidence.chunk_kind == "safety" and query_terms.intersection(SAFETY_QUERY_TERMS):
        score += 0.65
    if evidence.safety_level in {"technician", "safety-critical"} and query_terms.intersection(
        SAFETY_QUERY_TERMS
    ):
        score += 0.45

    for alarm_code in evidence.alarm_codes:
        normalized_code = normalize_for_search(alarm_code)
        if normalized_code and normalized_code in normalized_query:
            score += 1.2
    for error_code in re.findall(r"\berror\s+\d+\b", query.lower()):
        if normalize_for_search(error_code) in normalized_excerpt:
            score += 1.5

    excerpt = evidence.excerpt.lower()
    title = title_text.lower()
    if _is_procedure_like(excerpt):
        score += 0.45
    if _is_figure_only_candidate(title, excerpt):
        score -= 0.7
    if "how" in query.lower() and any(verb in excerpt for verb in ACTION_VERBS):
        score += 0.25

    return score


def manual_retrieval_query(state: GraphState, telemetry: TelemetrySnapshot | None) -> str:
    """Resolve references such as "the active alarm" before manual retrieval.

    The operator can see the current alarm directly above the composer and will
    naturally refer to it without repeating the full mnemonic. Searching only
    that shorthand favors generic alarm-table passages. For users allowed to
    read operational data, append the recorded alarm so retrieval, answer
    selection, and troubleshooting all rank evidence for the same condition.
    """
    query = state.operator_context
    if telemetry is None or not telemetry.has_active_alarm:
        return query

    if check_domain(state.access, Domain.OPERATIONAL, machine_id=state.machine_id) is not None:
        return query

    requested_code = extract_alarm_code(query)
    if requested_code:
        if not contains_alarm_code(telemetry.active_alarm, requested_code):
            return query
    else:
        normalized = normalize_for_search(query)
        alarm_reference = any(
            phrase in normalized
            for phrase in (
                "active alarm",
                "current alarm",
                "this alarm",
                "emergency alarm",
                "repeated alarm",
                "clear alarm",
                "clearing alarm",
                "reset alarm",
                "resetting alarm",
            )
        )
        if not alarm_reference:
            return query

    return f"{query}\nCurrent recorded alarm: {telemetry.active_alarm}."


def _is_procedure_like(text: str) -> bool:
    """True when the passage reads as steps rather than description."""
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
    """A caption or a bare table is a pointer to something, not an answer."""
    normalized_title = normalize_for_search(title)
    if normalized_title.startswith("fig"):
        return True

    tableish = excerpt.count("|") >= 2 or "spring kgf" in excerpt
    has_steps = sum(1 for verb in ACTION_VERBS if verb in excerpt) >= 2
    return tableish and not has_steps
