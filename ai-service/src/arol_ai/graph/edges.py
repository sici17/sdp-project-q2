import re

from arol_ai.alarm_codes import extract_alarm_code
from arol_ai.evaluators.safety import requires_safety_grounding
from arol_ai.graph.state import AgentName

IntentName = str

DOC_TERMS = (
    "manual",
    "procedure",
    "instruction",
    "alarm",
    "error",
    "inspection",
    "inspect",
    "check",
    "daily",
    "summarize",
    "summary",
    "troubleshoot",
    "troubleshooting",
    "fault",
    # Manual work described by the action it names rather than by the word
    # "procedure". A question about positioning a sensor is a manual question,
    # and without these it reached the manual only by accident.
    "position",
    "positioning",
    "adjust",
    "adjustment",
    "calibrate",
    "calibration",
    "align",
    "alignment",
    "install",
    "installation",
    "replace",
    "replacement",
    "remove",
    "removal",
    "mount",
    "mounting",
    "assemble",
    "disassemble",
    "clean",
    "cleaning",
    "changeover",
    "commissioning",
    # A value to set is a manual question, not a reading. "What torque setting
    # should I use?" matched TELEMETRY_TERMS on "torque" alone and answered with
    # the live torque instead of the figure the manual specifies.
    "setting",
    "settings",
    "specification",
    "spec",
    "tolerance",
    "interval",
    "diagram",
    "parameter",
)
TELEMETRY_TERMS = (
    "telemetry",
    "health",
    "rpm",
    "torque",
    "temperature",
    "sensor",
    "status",
    # How an operator actually asks what the machine is doing. Without these,
    # "what is this machine doing right now?" reached the manual alone, and the
    # model - given no readings and asked about the present - invented an alarm
    # code that does not exist.
    "doing",
    "running",
    "right now",
    "currently",
    "condition",
    "reading",
    "speed",
    "output",
    "throughput",
    "bph",
    "uptime",
)
TROUBLESHOOTING_TERMS = (
    "alarm",
    "diagnostic",
    "diagnose",
    "fault",
    "troubleshoot",
    "troubleshooting",
    "what should i check",
    "what should i inspect",
)
BUSINESS_TERMS = (
    "contract",
    "warranty",
    "sla",
    "service",
    "order",
    "maintenance",
    # The commercial half of the dataset: quotations and their revisions, and
    # what a machine cost. Without these, a question about quotes is routed to
    # the manual, which cannot answer it and, worse, never reaches the check
    # that would decline it for a technician.
    "quote",
    "quotation",
    "price",
    "pricing",
    "cost",
    "revision",
    "discount",
    "offer",
    "invoice",
    "purchase",
    "spare part",
    "spare parts",
    # Asking AROL to come out. Operators call this an intervention, a callout or
    # raising a ticket; none of it reads as "maintenance" or "order", so without
    # these the question was classified as not machine-related at all.
    "intervention",
    "callout",
    "call out",
    "site visit",
    "support",
    "assistance",
    "ticket",
    "claim",
)
CONVERSATION_EXACT = {
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "yes",
    "no",
    "nope",
}
CONVERSATION_TERMS = (
    "how are you",
    "who are you",
    "what can you do",
)

# "How do I ..." asks for a procedure. On its own this is deliberately not
# enough to reach the manual - see MACHINE_TERMS below - but when the question
# is already machine-related it says the operator wants the steps rather than a
# stored record. Without it, "how to order spare parts" answered with an order.
PROCEDURE_PHRASES = (
    "how to",
    "how do i",
    "how do we",
    "how can i",
    "how should i",
    "how is",
    "walk me through",
    "step by step",
    "steps to",
    "procedure for",
)

# A question without an explicit intent can still describe a machine component.
# Unknown text must not become a semantic manual search by default: even an
# unrelated question has a nearest passage in a vector index.
MACHINE_TERMS = (
    "machine",
    "capper",
    "capping",
    "bottle",
    "container",
    "cap",
    "sorter",
    "hopper",
    "chute",
    "conveyor",
    "roller",
    "spindle",
    "pneumatic",
    "lubrication",
    "lubricate",
    "grease",
    "air pressure",
    "compressed air",
    "motor",
    "actuator",
    "guard",
    "interlock",
    "emergency stop",
    "arol",
)


# Naming a topic in order to exclude it used to recruit the agent for it:
# "summarize the telemetry without mentioning alarms, maintenance, or
# troubleshooting" matched three term lists and fanned out to every agent, so
# the answer led with the alarm and carried five maintenance tickets and a
# diagnostic path. Keyword matching has no notion of negation; this gives it one.
#
# Precision matters more than recall here, the opposite of the safety
# predicate: suppressing an intent silently removes content, so only explicit
# exclusion phrasing counts. "no alarms?" is a question, not an exclusion.
NEGATION_CUES = (
    r"without",
    r"excluding",
    r"except(?:\s+for)?",
    r"other\s+than",
    r"a(?:side|part)\s+from",
    r"do(?:n't|\s+not)\s+(?:mention|include|discuss|list|show|talk\s+about)",
    r"no\s+need\s+(?:for|to\s+mention)",
    r"(?:skip|omit|ignore|leave\s+out)\s+(?:the\s+)?",
    r"(?:but|and)\s+not",
)
_NEGATION = re.compile(r"\b(?:" + "|".join(NEGATION_CUES) + r")\b")
# The exclusion runs to the end of the sentence, or to the point where the
# operator starts asking for something again. A compound request that resumes
# some other way over-scopes, which drops an agent rather than adding one.
_SCOPE_END = re.compile(
    r"[.!?;]|,?\s+(?:and|also|plus)\s+(?:include|show|add|give|tell|summari[sz]e)\b"
)


def _without_negated_spans(text: str) -> str:
    """The question with its excluded topics removed.

    Positive matching runs on this, so a term that appears only inside an
    exclusion never triggers the agent that would have answered about it.
    """
    kept: list[str] = []
    index = 0
    while True:
        cue = _NEGATION.search(text, index)
        if not cue:
            kept.append(text[index:])
            break
        kept.append(text[index : cue.start()])
        end = _SCOPE_END.search(text, cue.end())
        index = end.start() if end else len(text)
    return " ".join(part.strip() for part in kept if part.strip())


def _mentions(text: str, terms: tuple[str, ...]) -> bool:
    """True when any term appears as a whole word or phrase.

    Substring matching is not good enough here: "sla" appears inside
    "translate" and "order" inside "border", which sends a question to an agent
    that cannot answer it. A trailing plural is still the same word, though, and
    operators write "orders" and "quotes" far more often than the singular.
    """
    return any(re.search(rf"\b{re.escape(term)}s?\b", text) for term in terms)


def classify_intents(message: str) -> list[IntentName]:
    lower = message.lower().strip()
    intents: list[IntentName] = []

    if _is_conversation_message(lower):
        return ["conversation"]

    # Everything below matches on the question minus what it asked to exclude.
    # The safety check further down deliberately still reads the full text.
    scoped = _without_negated_spans(lower)

    # A bare code such as "AL031" is both a data question and, potentially, a
    # manual question. It must not fall through to semantic manual search only:
    # the exact definition lives in the machine's recorded alarm history.
    if extract_alarm_code(scoped):
        intents.extend(["documentation", "telemetry"])

    if _mentions(scoped, DOC_TERMS):
        intents.append("documentation")

    if _mentions(scoped, TELEMETRY_TERMS) or re.search(r"\balarm", scoped):
        intents.append("telemetry")

    if _mentions(scoped, TROUBLESHOOTING_TERMS):
        intents.append("troubleshooting")

    if _mentions(scoped, BUSINESS_TERMS):
        intents.append("business")

    # Deliberately after the other checks and before the fallback: it promotes a
    # question that already has an intent, and never invents one. "How do I make
    # coffee?" still reaches clarification rather than searching the manual.
    if intents and "documentation" not in intents and _mentions(scoped, PROCEDURE_PHRASES):
        intents.insert(0, "documentation")

    # A question about restarting a stopped machine is answered from what the
    # machine is actually doing, never from manual passages alone. Keyword
    # routing sent "why is the machine stopped, and can I safely restart it?"
    # to the manual by itself: no telemetry, so it never saw the standing
    # emergency stop, and no diagnostic steps, so the safety gate downstream
    # had nothing to fire on. Forced here rather than left to the wording.
    if requires_safety_grounding(lower):
        for intent in ("telemetry", "troubleshooting"):
            if intent not in intents:
                intents.append(intent)

    if not intents:
        intents.append("documentation" if _mentions(scoped, MACHINE_TERMS) else "clarification")

    if "troubleshooting" in intents:
        if "documentation" not in intents:
            intents.insert(0, "documentation")
        if "telemetry" not in intents:
            troubleshooting_index = intents.index("troubleshooting")
            intents.insert(troubleshooting_index, "telemetry")

    return list(dict.fromkeys(intents))


def agents_for_intents(intents: list[IntentName]) -> list[AgentName]:
    """The agents a set of intents calls for, in a fixed order.

    Split out from ``route_agents`` so intents established some other way - the
    LLM fallback on the clarification path - reach the same mapping instead of
    a parallel one that can drift from it.
    """
    agents: list[AgentName] = ["supervisor"]
    if "conversation" in intents:
        return agents

    if "documentation" in intents:
        agents.append("doc-agent")

    if "telemetry" in intents:
        agents.append("telemetry-agent")

    if "troubleshooting" in intents:
        agents.append("troubleshooting-agent")

    if "business" in intents:
        agents.append("business-agent")

    return agents


def route_agents(message: str) -> list[AgentName]:
    return agents_for_intents(classify_intents(message))


def routing_reason(intents: list[IntentName], agents: list[AgentName]) -> str:
    if agents == ["supervisor"]:
        return f"Detected intents: {', '.join(intents)}. No specialized tools were needed."

    return f"Detected intents: {', '.join(intents)}. Routed to: {', '.join(agents[1:])}."


def _is_conversation_message(message: str) -> bool:
    normalized = message.strip(" .!?")
    return normalized in CONVERSATION_EXACT or any(term in message for term in CONVERSATION_TERMS)
