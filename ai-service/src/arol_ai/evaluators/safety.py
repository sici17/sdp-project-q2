import re

UNSAFE_ACTION_TERMS = (
    "bypass guard",
    "disable guard",
    "remove guard",
    "override interlock",
    "defeat interlock",
    "ignore alarm",
    "run with door open",
    "keep running with alarm",
    "disable emergency stop",
)
UNSAFE_ACTION_PATTERNS = (
    r"\b(?:bypass|disable|override|defeat|ignore|remove)\s+(?:the\s+)?(?:safety\s+)?(?:guards?|interlocks?|protections?|emergency\s+stop)\b",
    r"\bkeep\s+running\s+with\s+(?:the\s+)?(?:alarm|door\s+open)\b",
)
PROMPT_INJECTION_TERMS = (
    "ignore previous instructions",
    "ignore your instructions",
    "ignore the system prompt",
    "reveal the system prompt",
    "show the system prompt",
    "print the system prompt",
    "developer message",
    "jailbreak",
    "disable safety policy",
)


# Questions whose *answer* bears on safety, as opposed to requests to defeat it.
#
# "How do I override the interlock" is caught above and refused. "Can I safely
# restart it?" must not be refused - it is exactly what an operator standing at
# a stopped machine should ask - but it must never be answered casually, from
# manual passages alone, by a model free to write prose.
#
# Deliberately high recall. A false positive costs a slower, more cautious
# answer. A false negative cost an operator a fabricated "there are no leaks
# from the pneumatic circuit" on a machine with an unresolved emergency stop.
RESTART_SAFETY_PATTERNS = (
    # Putting the machine back into production.
    r"\bre-?start(?:ing|ed|s)?\b",
    r"\bstart(?:ing)?\s+(?:it|the|up|again)\b",
    r"\bresum(?:e|es|ing)\b",
    r"\b(?:turn|switch|power)(?:ing)?\s+(?:it\s+)?back\s+(?:on|up)\b",
    r"\bback\s+(?:in|into)\s+production\b",
    r"\brun\s+it\s+again\b",
    r"\bcan\s+i\s+(?:run|re-?start|start|resume|continue|operate)\b",
    # Asking whether an action is safe, in any phrasing.
    r"\bis\s+it\s+safe\b",
    r"\bsafe\s+to\b",
    r"\bsafely\s+(?:re-?start|start|resume|run|continue|operate)\b",
    r"\b(?:ok|okay)\s+to\s+(?:re-?start|start|run|resume|continue)\b",
    # Why the machine is down. The answer names a fault and implies what to do
    # about it, so it carries the same weight as asking to restart.
    r"\bwhy\s+(?:is|are|has|have|did|was|were)\b.{0,40}?\b(?:stop|stopped|stopping|halt|halted|shut\s*down)\b",
    r"\bmachine\s+(?:is\s+|has\s+)?stopped\b",
)

_RESTART_SAFETY = tuple(re.compile(pattern) for pattern in RESTART_SAFETY_PATTERNS)


def requires_safety_grounding(message: str) -> bool:
    """True when the answer must be grounded rather than written by a model.

    This does not block the question. It forces the machine's real state into
    the answer and holds the reply to the deterministic composer, because the
    operator is deciding whether to put a stopped machine back into production.
    """
    return any(pattern.search(message.lower()) for pattern in _RESTART_SAFETY)


def classify_blocked_request(message: str) -> str | None:
    lower = message.lower()

    if any(term in lower for term in UNSAFE_ACTION_TERMS) or any(
        re.search(pattern, lower) for pattern in UNSAFE_ACTION_PATTERNS
    ):
        return "safety"

    if any(term in lower for term in PROMPT_INJECTION_TERMS):
        return "prompt-injection"

    return None


def build_safety_response(machine_label: str) -> str:
    return (
        f"I cannot help bypass or disable safety protections on {machine_label}. "
        "Stop the machine, keep guards and interlocks active, record the alarm code and telemetry, "
        "and escalate to a qualified AROL technician before restarting."
    )


def build_guardrail_response(machine_label: str, reason: str | None) -> str:
    if reason == "prompt-injection":
        return (
            f"I cannot follow instructions that try to override the assistant or safety rules for "
            f"{machine_label}. I can still help with machine procedures, telemetry, warranty, and "
            "safe escalation using cited operational evidence."
        )

    return build_safety_response(machine_label)
