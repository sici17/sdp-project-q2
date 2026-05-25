"""A model asked to route only the questions keywords could not place.

Keyword routing decides everything that matters for safety, and cannot read a
question it has no words for - a Spanish maintenance request, or an English one
typed with gloves on. Both fell through to ``clarification``: a refusal telling
an operator their machine question was not a machine question.

So the model runs on that path and no other. The guardrail has already returned
by then, ``requires_safety_grounding`` still runs on the full text afterwards,
and the reply is confined to four intents - so the worst a confused model can do
is name the wrong specialist agent, which keyword routing can do too.
"""

from arol_ai.llm.providers import LLMProvider, LLMProviderError

#: Everything the fallback is allowed to say. Deliberately excludes
#: ``conversation``, ``safety`` and ``prompt-injection``: those are decided
#: before this runs and are not the model's to revisit.
ROUTABLE_INTENTS = ("documentation", "telemetry", "troubleshooting", "business")

_SYSTEM = (
    "You label maintenance questions about industrial bottle-capping machines. "
    "Answer with a comma-separated list drawn only from these four labels: "
    "documentation, telemetry, troubleshooting, business. "
    "Answer NONE only when the question has nothing to do with a machine. "
    "Answer with the labels and nothing else."
)

# A 3B model given rules alone answered NONE to almost everything, including a
# plain Italian question about open orders. Worked examples move it; the last
# two are the cases this fallback exists for, so they are the ones shown.
_GUIDE = """documentation = the manual, a procedure, a setting, a specification
telemetry = live readings, alarms, current machine condition
troubleshooting = diagnosing a fault, deciding what to check
business = quotes, orders, prices, maintenance tickets, service records

The question may be in any language and may contain typos. Translate it in your
head first, then label it. Only a question about something other than this
machine is NONE.

Question: What is the capital of France?
Labels: NONE

Question: Show me the latest readings
Labels: telemetry

Question: quali sono gli ordini aperti?
Labels: business

Question: wghat si teh torqeu settign
Labels: documentation

Question: """


def classify_with_llm(message: str, provider: LLMProvider | None) -> list[str]:
    """Intents for a question the keyword classifier could not place.

    Returns an empty list whenever the answer cannot be trusted - no provider,
    a provider error, an unparseable reply, or a reply naming nothing in the
    vocabulary. The caller keeps its clarification refusal in that case, so a
    failure here costs the behaviour that was already going to happen.
    """
    if provider is None or not message.strip():
        return []

    # A provider without `complete` can only rewrite answers: asked to route, it
    # returns prose that happens to contain the word "telemetry".
    complete = getattr(provider, "complete", None)
    if complete is None:
        return []

    try:
        raw = complete(_SYSTEM, f"{_GUIDE}{message}", max_tokens=24)
    except LLMProviderError:
        return []
    except Exception:
        # A router is not worth failing a turn over.
        return []

    return _parse(raw)


def _parse(raw: str) -> list[str]:
    lowered = str(raw or "").strip().lower()
    if not lowered or "none" in lowered.split():
        return []

    # The reply has to look like the label list it was asked for. A model that
    # answers in sentences is not routing - it is doing something else - and
    # "There are no telemetry findings to report." must never be read as a vote
    # for the telemetry agent.
    if len(lowered) > 64 or any(mark in lowered for mark in (".", "!", "?", ":")):
        return []

    found = [intent for intent in ROUTABLE_INTENTS if intent in lowered]

    # Naming everything is agreement with the prompt, not a reading of the
    # question.
    if len(found) == len(ROUTABLE_INTENTS):
        return []

    return found
