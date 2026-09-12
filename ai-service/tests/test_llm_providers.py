import json
from dataclasses import replace
from urllib.error import URLError

import pytest

from arol_ai.config import get_settings
from arol_ai.llm.providers import (
    AzureOpenAIChatProvider,
    DeterministicLLMProvider,
    LLMProviderConfigurationError,
    LLMProviderResponseError,
    OllamaChatProvider,
    build_llm_provider,
)
from arol_ai.prompts.registry import load_prompt


def test_deterministic_provider_returns_grounded_draft() -> None:
    provider = DeterministicLLMProvider()

    assert (
        provider.synthesize({"draft": "Use the cited manual step."}) == "Use the cited manual step."
    )


def test_ollama_provider_posts_chat_request_to_configured_model() -> None:
    def fake_transport(api_request, timeout_seconds):
        payload = json.loads(api_request.data.decode("utf8"))
        assert api_request.full_url == "http://ollama.test/api/chat"
        assert payload["model"] == "qwen2.5:0.5b-instruct"
        assert payload["stream"] is False
        assert payload["messages"][0]["role"] == "system"
        # The system message comes from the prompt registry, so what a reviewer
        # reads at /api/v1/prompts is what the model was actually sent.
        assert payload["messages"][0]["content"] == load_prompt("answer_synthesizer")
        assert (
            "Never introduce a fact that is not in the supplied context"
            in (payload["messages"][0]["content"])
        )
        assert timeout_seconds == 45
        return b'{"message": {"content": "Grounded Ollama answer."}}'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="qwen2.5:0.5b-instruct",
        transport=fake_transport,
    )

    assert provider.synthesize({"draft": "fallback"}) == "Grounded Ollama answer."


def test_azure_provider_posts_chat_request_to_configured_deployment() -> None:
    def fake_transport(api_request, timeout_seconds):
        payload = json.loads(api_request.data.decode("utf8"))
        assert api_request.full_url == (
            "https://azure.test/openai/deployments/q2/chat/completions?api-version=2024-10-21"
        )
        assert payload["temperature"] == 0
        assert payload["messages"][1]["role"] == "user"
        assert timeout_seconds == 45
        return b'{"choices": [{"message": {"content": "Grounded Azure answer."}}]}'

    provider = AzureOpenAIChatProvider(
        endpoint="https://azure.test",
        deployment="q2",
        api_key="secret",
        transport=fake_transport,
    )

    assert provider.synthesize({"draft": "fallback"}) == "Grounded Azure answer."


def test_deterministic_provider_streams_multiple_structural_deltas() -> None:
    provider = DeterministicLLMProvider()

    chunks = list(provider.synthesize_stream({"draft": "First sentence. Second sentence."}))

    assert len(chunks) > 1
    assert "".join(chunks) == "First sentence. Second sentence."


def test_ollama_provider_uses_native_streaming_response() -> None:
    def fake_stream_transport(api_request, timeout_seconds):
        payload = json.loads(api_request.data.decode("utf8"))
        assert payload["stream"] is True
        assert timeout_seconds == 45
        yield b'{"message":{"content":"Grounded "}}\n'
        yield b'{"message":{"content":"stream."},"done":true}\n'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        stream_transport=fake_stream_transport,
    )

    assert list(provider.synthesize_stream({"draft": "fallback"})) == [
        "Grounded ",
        "stream.",
    ]


def test_azure_provider_uses_configurable_api_version() -> None:
    captured = {}

    def fake_transport(api_request, _timeout_seconds):
        captured["url"] = api_request.full_url
        return b'{"choices": [{"message": {"content": "ok"}}]}'

    provider = AzureOpenAIChatProvider(
        endpoint="https://azure.test",
        deployment="q2 deployment",
        api_key="secret",
        api_version="2025-01-01-preview",
        transport=fake_transport,
    )

    assert provider.synthesize({"draft": "fallback"}) == "ok"
    assert captured["url"].endswith(
        "/q2%20deployment/chat/completions?api-version=2025-01-01-preview"
    )


def test_requested_azure_provider_never_silently_downgrades() -> None:
    settings = replace(
        get_settings(),
        llm_provider="azure_openai",
        azure_openai_endpoint=None,
        azure_openai_deployment=None,
        azure_openai_api_key=None,
    )

    with pytest.raises(LLMProviderConfigurationError, match="missing required settings"):
        build_llm_provider(settings)


def test_provider_network_errors_are_normalized_without_url_or_credentials() -> None:
    def failed_transport(_request, _timeout_seconds):
        raise URLError("private-host.internal")

    provider = AzureOpenAIChatProvider(
        endpoint="https://private-host.internal",
        deployment="secret-deployment",
        api_key="super-secret",
        transport=failed_transport,
    )

    with pytest.raises(LLMProviderResponseError, match="network request failed") as error:
        provider.synthesize({"draft": "fallback"})

    assert "private-host" not in str(error.value)
    assert "super-secret" not in str(error.value)


def test_ollama_request_bounds_generation_and_penalises_repetition() -> None:
    captured = {}

    def fake_transport(api_request, _timeout_seconds):
        captured["options"] = json.loads(api_request.data.decode("utf8"))["options"]
        return b'{"message": {"content": "Grounded answer."}}'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=fake_transport,
    )
    provider.synthesize({"draft": "fallback"})

    options = captured["options"]
    # Greedy decoding with no penalty is what let the model restate the same
    # maintenance step until the request timed out.
    assert options["temperature"] == 0
    assert options["repeat_penalty"] > 1
    # Long enough for a full alarm procedure - 320 truncated one mid-word - and
    # still bounded, because num_ctx stays at the server default.
    assert 512 <= options["num_predict"] <= 700
    # num_ctx must stay at the server default: raising it was enough to have the
    # container killed on an 8 GB machine.
    assert "num_ctx" not in options


def test_ollama_stream_stops_when_the_model_locks_into_a_cycle() -> None:
    step = "Clean the magnetic rings and apply a layer of lubricant on all surfaces."

    def fake_stream_transport(_api_request, _timeout_seconds):
        for _ in range(50):
            yield json.dumps({"message": {"content": f"{step}\n"}}).encode() + b"\n"

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        stream_transport=fake_stream_transport,
    )

    emitted = []
    with pytest.raises(LLMProviderResponseError):
        for delta in provider.synthesize_stream({"draft": "fallback"}):
            emitted.append(delta)

    # The caller falls back to the grounded draft, and the operator is not shown
    # fifty copies of the same instruction.
    assert len(emitted) < 5


def test_ollama_rejects_a_non_streamed_answer_that_repeats_itself() -> None:
    step = "Clean the release seats of the head central body; apply lubricant."

    def fake_transport(_api_request, _timeout_seconds):
        looped = "\n".join([step] * 30)
        return json.dumps({"message": {"content": looped}}).encode()

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=fake_transport,
    )

    with pytest.raises(LLMProviderResponseError):
        provider.synthesize({"draft": "fallback"})


def test_ollama_keeps_an_answer_that_merely_reuses_short_formatting() -> None:
    def fake_stream_transport(_api_request, _timeout_seconds):
        # Repeated bullet markers and blank lines are formatting, not a cycle.
        for chunk in ["- step one\n", "\n", "- step two\n", "\n", "- step three\n"]:
            yield json.dumps({"message": {"content": chunk}}).encode() + b"\n"

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        stream_transport=fake_stream_transport,
    )

    assert "".join(provider.synthesize_stream({"draft": "fallback"})).count("step") == 3


def _full_answer_context() -> dict:
    """An answer context shaped like the one the graph actually builds."""
    passage = "Check the closure gripper adjustment and the slotted ring nut. " * 30
    return {
        "draft": "Checked: M-EURO-VP-IES (17478)\n\nTelemetry\n- ...",
        "structuredAnswer": {"sections": [{"title": "Telemetry", "bullets": ["Running at 88%."]}]},
        "machineLabel": "M-EURO-VP-IES (17478)",
        "userMessage": "Why is this machine raising repeated alarms?",
        "accessDenials": [{"domain": "commercial", "reason": "visibility_denied"}],
        "agentTrace": ["supervisor", "doc-agent", "telemetry-agent"],
        "intents": ["troubleshooting"],
        "evidence": [
            {
                "title": f"Section {index}",
                "page": index,
                "sourceUri": "/manuals/17478_manual_EN.pdf",
                "excerpt": passage,
            }
            for index in range(6)
        ],
        "toolCalls": [{"name": "manual.search", "status": "ok", "durationMs": 412}] * 8,
        "diagnosticSteps": [{"title": "Record active alarm", "detail": passage}],
        "recommendedActions": [{"title": "Escalate", "detail": passage}],
        "telemetry": {"machineId": "MCH-0004", "temperatureC": 22.4},
        "contract": {"deliveryDate": "2019-07-22", "acquisitionCost": 52600},
        "orders": [{"orderId": f"ORD-{index}", "notes": passage} for index in range(4)],
        "serviceHistory": [{"ticketId": f"TCK-{index}", "notes": passage} for index in range(5)],
        "quotes": [{"quoteId": f"QTE-{index}", "description": passage} for index in range(5)],
        "messages": [{"role": "user", "content": passage}] * 8,
    }


def test_the_model_is_sent_the_draft_to_rewrite_not_the_whole_turn() -> None:
    captured = {}

    def fake_transport(api_request, _timeout_seconds):
        captured["payload"] = json.loads(api_request.data.decode("utf8"))
        return b'{"message": {"content": "Grounded answer."}}'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=fake_transport,
    )
    context = _full_answer_context()
    provider.synthesize(context)

    sent = json.loads(captured["payload"]["messages"][1]["content"])

    # The content the prompt says it works from, and the refusal it must keep.
    assert sent["structuredAnswer"] == context["structuredAnswer"]
    assert sent["userMessage"] == context["userMessage"]
    assert sent["accessDenials"] == context["accessDenials"]

    # Everything the composer already folded into structuredAnswer, or that is
    # routing bookkeeping, stays out of the window.
    for key in (
        "draft",
        "agentTrace",
        "intents",
        "toolCalls",
        "diagnosticSteps",
        "recommendedActions",
        "telemetry",
        "contract",
        "orders",
        "serviceHistory",
        "quotes",
        "messages",
    ):
        assert key not in sent, f"{key} should not be sent to the model"


def test_citations_survive_the_trim_but_their_excerpts_are_bounded() -> None:
    captured = {}

    def fake_transport(api_request, _timeout_seconds):
        captured["payload"] = json.loads(api_request.data.decode("utf8"))
        return b'{"message": {"content": "Grounded answer."}}'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=fake_transport,
    )
    provider.synthesize(_full_answer_context())

    sent = json.loads(captured["payload"]["messages"][1]["content"])
    assert len(sent["evidence"]) <= 3
    for item in sent["evidence"]:
        # The citation identity is what must not be dropped; the passage is not.
        assert item["sourceUri"] == "/manuals/17478_manual_EN.pdf"
        assert item["page"] is not None
        assert len(item["excerpt"]) <= 240


def test_model_evidence_excludes_adjacent_alarm_rows() -> None:
    captured = {}

    def fake_transport(api_request, _timeout_seconds):
        captured["payload"] = json.loads(api_request.data.decode("utf8"))
        return b'{"message": {"content": "Grounded answer."}}'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=fake_transport,
    )
    provider.synthesize(
        {
            "userMessage": "How do I reset the active alarm?",
            "structuredAnswer": {
                "sections": [
                    {
                        "title": "Telemetry",
                        "bullets": ["Active alarm AL019_CAPS_SORTER_UPPER_DOOR_OPEN."],
                    }
                ]
            },
            "evidence": [
                {
                    "title": "MESSAGE",
                    "page": 83,
                    "sourceUri": "/manual.pdf",
                    "excerpt": (
                        "17 LOW AIR PRESSURE Check the pneumatic supply. "
                        "19 CAPS SORTER UPPER DOOR OPEN Close the door and press RESET. "
                        "20 HEIGHT MAXIMUM LIMIT REACHED Stop height adjustment."
                    ),
                }
            ],
        }
    )

    sent = json.loads(captured["payload"]["messages"][1]["content"])
    excerpt = sent["evidence"][0]["excerpt"]
    assert "CAPS SORTER UPPER DOOR OPEN" in excerpt
    assert "LOW AIR PRESSURE" not in excerpt
    assert "HEIGHT MAXIMUM" not in excerpt


def test_the_trimmed_prompt_fits_the_local_model_context_window() -> None:
    captured = {}

    def fake_transport(api_request, _timeout_seconds):
        captured["payload"] = json.loads(api_request.data.decode("utf8"))
        return b'{"message": {"content": "Grounded answer."}}'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=fake_transport,
    )
    provider.synthesize(_full_answer_context())

    messages = captured["payload"]["messages"]
    characters = sum(len(message["content"]) for message in messages)
    # qwen2.5:1.5b-instruct serves 4096 tokens. At the ~4 chars/token this text
    # runs to, staying under 12000 characters leaves the generation room that
    # the untrimmed prompt did not.
    assert characters < 12000, f"prompt is {characters} chars, which the server will truncate"


def test_ollama_stream_stops_when_the_model_restates_a_whole_block() -> None:
    """The failure a longer answer actually produces.

    Raising num_predict so a four-step alarm procedure fits gave the model room
    to finish and then write the same block again as "Step 5". No single line
    reaches the repeat limit - each is said exactly twice - so only counting how
    many distinct lines came back sees it.
    """
    block = [
        "Ensure that the machine is stopped before any further action.",
        "Verify that all safety guards are in place, interlocked, or fixed.",
        "Press one of the red mushroom-shaped emergency buttons on the panel.",
    ]

    def fake_stream_transport(_api_request, _timeout_seconds):
        for heading, lines in (("Step 3", block), ("Step 5", block)):
            yield json.dumps({"message": {"content": f"### {heading}\n"}}).encode() + b"\n"
            for line in lines:
                yield json.dumps({"message": {"content": f"{line}\n"}}).encode() + b"\n"

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        stream_transport=fake_stream_transport,
    )

    with pytest.raises(LLMProviderResponseError):
        list(provider.synthesize_stream({"draft": "fallback"}))


def test_ollama_stream_allows_a_line_that_recurs_once() -> None:
    """A safety line restated once is formatting, not a loop.

    The block check must not fire on an answer that repeats a single caution at
    the end, which is how several grounded drafts are written.
    """

    def fake_stream_transport(_api_request, _timeout_seconds):
        for line in (
            "Check the closure head torque against the setting in the manual.",
            "If the alarm remains, escalate to a qualified AROL technician.",
            "Confirm the guard interlock is closed before restarting the line.",
            "If the alarm remains, escalate to a qualified AROL technician.",
        ):
            yield json.dumps({"message": {"content": f"{line}\n"}}).encode() + b"\n"

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        stream_transport=fake_stream_transport,
    )

    assert "escalate" in "".join(provider.synthesize_stream({"draft": "fallback"}))


def test_complete_asks_the_model_the_callers_own_question() -> None:
    """`synthesize` always sends the answer-synthesiser prompt.

    The intent router asked it to classify a question and got back a rewritten
    machine summary - "There are no telemetry findings to report." - which a
    substring parser happily read as a vote for the telemetry agent.
    """
    captured = {}

    def fake_transport(api_request, _timeout_seconds):
        captured["payload"] = json.loads(api_request.data.decode("utf8"))
        return b'{"message": {"content": "documentation, business"}}'

    provider = OllamaChatProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=fake_transport,
    )

    reply = provider.complete("Label the question.", "Question: how do I order parts?")

    assert reply == "documentation, business"
    roles = [message["role"] for message in captured["payload"]["messages"]]
    assert roles == ["system", "user"]
    assert captured["payload"]["messages"][0]["content"] == "Label the question."
    assert "how do I order parts?" in captured["payload"]["messages"][1]["content"]
    # A label needs a handful of tokens, and this sits on the turn's path.
    assert captured["payload"]["options"]["num_predict"] <= 64


def test_the_deterministic_provider_declines_to_route() -> None:
    assert DeterministicLLMProvider().complete("system", "user") == ""
