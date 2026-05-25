from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from arol_ai.access import ANONYMOUS, AccessContext, AccessDenied
from arol_ai.tools.base import ToolCallRecord

AgentName = Literal[
    "supervisor",
    "doc-agent",
    "telemetry-agent",
    "troubleshooting-agent",
    "business-agent",
]
ActionPriority = Literal["immediate", "next", "escalate"]
SafetyLevel = Literal["operator", "technician", "safety-critical"]
LOW_CONFIDENCE_THRESHOLD = 0.65


@dataclass(frozen=True)
class Evidence:
    source: str
    title: str
    excerpt: str
    page: int | None = None
    #: The number printed on that page of the manual. Differs from ``page``,
    #: which is the PDF index the viewer navigates by, whenever the manual has
    #: front matter. Operators read the printed one off the paper copy.
    printed_page: int | None = None
    confidence: float | None = None
    manual_version: str | None = None
    language: str | None = None
    source_uri: str | None = None
    chunk_id: str | None = None
    section: str | None = None
    score: float | None = None
    chunk_kind: str | None = None
    topics: tuple[str, ...] = ()
    alarm_codes: tuple[str, ...] = ()
    safety_level: SafetyLevel | None = None

    def to_dict(self) -> dict:
        payload = {
            "source": self.source,
            "title": self.title,
            "excerpt": self.excerpt,
        }
        if self.page is not None:
            payload["page"] = self.page
        if self.printed_page is not None:
            payload["printedPage"] = self.printed_page
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.manual_version is not None:
            payload["manualVersion"] = self.manual_version
        if self.language is not None:
            payload["language"] = self.language
        if self.source_uri is not None:
            payload["sourceUri"] = self.source_uri
        if self.chunk_id is not None:
            payload["chunkId"] = self.chunk_id
        if self.section is not None:
            payload["section"] = self.section
        if self.score is not None:
            payload["score"] = self.score
        if self.chunk_kind is not None:
            payload["chunkKind"] = self.chunk_kind
        if self.topics:
            payload["topics"] = list(self.topics)
        if self.alarm_codes:
            payload["alarmCodes"] = list(self.alarm_codes)
        if self.safety_level is not None:
            payload["safetyLevel"] = self.safety_level
        return payload


@dataclass(frozen=True)
class ActionItem:
    label: str
    priority: ActionPriority
    requires_technician: bool
    source: str

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "priority": self.priority,
            "requiresTechnician": self.requires_technician,
            "source": self.source,
        }


@dataclass(frozen=True)
class DiagnosticStep:
    label: str
    detail: str
    priority: ActionPriority
    requires_technician: bool
    source: str
    evidence_title: str | None = None
    page: int | None = None
    printed_page: int | None = None
    expected_outcome: str | None = None
    pass_follow_up: str | None = None
    fail_follow_up: str | None = None
    safety_level: SafetyLevel | None = None
    required_role: str | None = None
    step_id: str | None = None
    pass_next_step_id: str | None = None
    fail_next_step_id: str | None = None

    def to_dict(self) -> dict:
        payload = {
            "label": self.label,
            "detail": self.detail,
            "priority": self.priority,
            "requiresTechnician": self.requires_technician,
            "source": self.source,
        }
        if self.printed_page is not None:
            payload["printedPage"] = self.printed_page
        if self.evidence_title is not None:
            payload["evidenceTitle"] = self.evidence_title
        if self.page is not None:
            payload["page"] = self.page
        if self.expected_outcome is not None:
            payload["expectedOutcome"] = self.expected_outcome
        if self.pass_follow_up is not None:
            payload["passFollowUp"] = self.pass_follow_up
        if self.fail_follow_up is not None:
            payload["failFollowUp"] = self.fail_follow_up
        if self.safety_level is not None:
            payload["safetyLevel"] = self.safety_level
        if self.required_role is not None:
            payload["requiredRole"] = self.required_role
        if self.step_id is not None:
            payload["stepId"] = self.step_id
        if self.pass_next_step_id is not None:
            payload["passNextStepId"] = self.pass_next_step_id
        if self.fail_next_step_id is not None:
            payload["failNextStepId"] = self.fail_next_step_id
        return payload


@dataclass
class GraphState:
    session_id: str
    machine_id: str
    user_message: str
    messages: list[dict] = field(default_factory=list)
    agent_trace: list[AgentName] = field(default_factory=lambda: ["supervisor"])
    intents: list[str] = field(default_factory=list)
    routing_reason: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    diagnostic_steps: list[DiagnosticStep] = field(default_factory=list)
    recommended_actions: list[ActionItem] = field(default_factory=list)
    maintenance_assessment: list[str] = field(default_factory=list)
    #: Front-matter pages in this machine's manual. Set once per turn from the
    #: manifest; ``resolve_printed_pages`` spends it.
    printed_page_offset: int = 0
    #: The keyword classifier could not place this question and the model
    #: named the intents instead. Surfaced in the routing reason so a
    #: trace says which classifier decided.
    routed_by_model: bool = False
    safety_blocked: bool = False
    #: The question bears on restarting a stopped machine. Not a refusal:
    #: the turn runs, but the machine's real state is forced into it and the
    #: answer stays with the grounded composer.
    safety_sensitive: bool = False
    guardrail_reason: str | None = None
    answer: str = ""
    answer_context: dict | None = None
    #: Who is asking. Agents check this before their tools touch any data.
    access: AccessContext = ANONYMOUS
    #: Domains this turn asked for but the user may not see. Rendered as an
    #: explicit refusal, never as an empty result.
    access_denials: list[AccessDenied] = field(default_factory=list)

    def resolve_printed_pages(self) -> None:
        """Fill in the page number the operator will read off the paper.

        ``page`` is the PDF index the viewer navigates by and stays untouched;
        ``printed_page`` is what the manual prints on that sheet. Where the two
        differ - front matter, on most of the corpus - citing the index sent an
        operator to the wrong procedure in a printed manual.

        Manual sources only. A telemetry or business record has no page, and an
        offset applied to one would be meaningless.
        """
        offset = self.printed_page_offset
        if offset <= 0:
            return

        def printed(item):
            if item.source != "manual" or item.page is None:
                return item
            number = item.page - offset
            if number < 1:
                # Front matter, which the manual does not number at all. This
                # used to clamp to 1 and report a printed page that does not
                # exist - PDF page 3 of an 11-page front section came back as
                # "page 1". With none set, the citation falls back to the index.
                return item
            return replace(item, printed_page=number)

        self.evidence = [printed(item) for item in self.evidence]
        self.diagnostic_steps = [printed(step) for step in self.diagnostic_steps]

    def deny(self, denial: AccessDenied) -> None:
        if not any(
            existing.domain == denial.domain and existing.reason == denial.reason
            for existing in self.access_denials
        ):
            self.access_denials.append(denial)

    @property
    def tenancy_denied(self) -> bool:
        """The machine belongs to another company, so nothing about it is answerable.

        Unlike a visibility refusal, which withholds one domain and answers the
        rest, this one closes the whole turn: there is no part of another
        tenant's machine this identity may read.
        """
        return any(denial.reason == "machine_not_in_company" for denial in self.access_denials)

    @property
    def operator_context(self) -> str:
        return self.user_message

    def to_response(self) -> dict:
        confidence = self.answer_confidence()
        review_reasons = self.review_reasons(confidence)

        return {
            "message": {
                "id": str(uuid4()),
                "role": "assistant",
                "content": self.answer,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
            "agentTrace": self.agent_trace,
            "intents": self.intents,
            "routingReason": self.routing_reason,
            "evidence": [item.to_dict() for item in self.evidence],
            "toolCalls": [item.to_dict() for item in self.tool_calls],
            "diagnosticSteps": [item.to_dict() for item in self.diagnostic_steps],
            "recommendedActions": [item.to_dict() for item in self.recommended_actions],
            # Surfaced so the UI can show what was refused and why, rather than
            # leaving the operator to infer it from a shorter answer.
            "accessDenials": [item.to_dict() for item in self.access_denials],
            "answerConfidence": confidence,
            "reviewRequired": len(review_reasons) > 0,
            "reviewReasons": review_reasons,
        }

    def answer_confidence(self) -> float:
        if self.safety_blocked:
            return 0.2

        if any(tool_call.status == "error" for tool_call in self.tool_calls):
            return 0.4

        if "doc-agent" in self.agent_trace and not any(
            item.source == "manual" for item in self.evidence
        ):
            return 0.45

        if "troubleshooting-agent" in self.agent_trace and not self.diagnostic_steps:
            return 0.5

        evidence_scores = [
            score
            for item in self.evidence
            for score in (item.confidence, item.score)
            if score is not None
        ]

        if evidence_scores:
            return round(max(0.0, min(1.0, sum(evidence_scores) / len(evidence_scores))), 3)

        if self.intents == ["conversation"]:
            return 0.95

        return 0.7

    def review_reasons(self, confidence: float | None = None) -> list[str]:
        reasons: list[str] = []
        confidence = self.answer_confidence() if confidence is None else confidence

        if self.safety_blocked and self.guardrail_reason:
            reasons.append(self.guardrail_reason)

        if confidence < LOW_CONFIDENCE_THRESHOLD:
            reasons.append("low-confidence")

        if any(tool_call.status == "error" for tool_call in self.tool_calls):
            reasons.append("tool-error")

        escalation_context = self.safety_blocked or "troubleshooting-agent" in self.agent_trace

        if escalation_context and any(
            action.priority == "immediate" for action in self.recommended_actions
        ):
            reasons.append("immediate-action")

        if escalation_context and any(
            action.priority == "escalate" for action in self.recommended_actions
        ):
            reasons.append("technician-escalation")

        if any(step.requires_technician for step in self.diagnostic_steps):
            reasons.append("diagnostic-escalation")

        return list(dict.fromkeys(reasons))
