"""The prompt registry, and an honest statement of what is actually a prompt.

Only one of these files is sent to a language model: the answer synthesizer,
which the LLM provider loads for its system message. The other four describe
what each agent is responsible for and what it must not do. Those rules are
enforced in code — routing, tool gating, the access checks — not by asking a
model to follow them, which is the only way they can be relied on.

They are kept, and served, because they are the specification the code is
written against. ``sent_to_model`` says which is which, so nobody reading the
registry endpoint mistakes a design document for a live prompt.
"""

from functools import lru_cache
from importlib import resources

PROMPT_NAMES = (
    "supervisor",
    "doc_agent",
    "telemetry_agent",
    "business_agent",
    "answer_synthesizer",
)

#: Prompts that are actually sent to a language model at runtime.
MODEL_PROMPTS = frozenset({"answer_synthesizer"})


def describe_prompts() -> list[dict]:
    return [
        {
            "name": name,
            "sentToModel": name in MODEL_PROMPTS,
            "role": (
                "System message for answer synthesis."
                if name in MODEL_PROMPTS
                else "Agent specification, enforced in code rather than by prompting."
            ),
        }
        for name in PROMPT_NAMES
    ]


@lru_cache(maxsize=len(PROMPT_NAMES))
def load_prompt(name: str) -> str:
    if name not in PROMPT_NAMES:
        raise ValueError(f"Unknown prompt: {name}")

    return resources.files("arol_ai.prompts").joinpath(f"{name}.md").read_text(encoding="utf8")
