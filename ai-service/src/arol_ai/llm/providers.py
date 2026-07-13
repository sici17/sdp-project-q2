import json
import re
from collections.abc import Iterator
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from arol_ai.config import Settings, get_settings
from arol_ai.prompts.registry import load_prompt
from arol_ai.rag.text import focus_alarm_passage

Transport = Callable[[Request, int], bytes]
StreamTransport = Callable[[Request, int], Iterator[bytes]]


class LLMProviderError(RuntimeError):
    """Base error for explicit provider configuration and execution failures."""


class LLMProviderConfigurationError(LLMProviderError):
    pass


class LLMProviderResponseError(LLMProviderError):
    pass


# A small instruct model decoded greedily will sometimes lock into a cycle and
# restate the same step until something stops it. `repeat_penalty` is what stops
# it forming; `_RepetitionGuard` is what aborts it when it forms anyway.
# Temperature stays at 0 so the same draft still rewrites the same way for
# evaluation runs.
#
# `num_predict` is the last-resort bound, not the anti-cycle mechanism. At 320 it
# was cutting legitimate answers mid-word: a four-step alarm procedure with an
# escalation block does not fit, and the operator was reading a sentence that
# stopped at "recorded at 202". The guard above is what a cycle runs into now,
# so this can be the length a real procedure needs.
#
# Kept below the Azure path's 700: `num_ctx` stays at the server default (2048
# for these models) and the grounded prompt carries manual excerpts, so the
# reply has to leave room for what it was grounded on.
#
# `num_ctx` is deliberately left at the server default. Raising it to 8192 on an
# 8 GB machine was enough to have the kernel kill the container mid-request.
OLLAMA_GENERATION_OPTIONS = {
    "temperature": 0,
    "repeat_penalty": 1.15,
    "num_predict": 640,
}

#: A repeated line shorter than this is ordinary formatting, not a cycle.
_LOOP_MIN_LINE_CHARS = 24
#: Emitting the same substantial line this many times is not prose any more.
_LOOP_REPEAT_LIMIT = 4
#: Distinct lines that may be said twice before the answer is restating itself.
#:
#: A tight cycle repeats one line many times, which the limit above catches. A
#: small model given room for a long procedure fails differently: it finishes,
#: then writes the whole block again under a new heading. Every line is repeated
#: exactly twice, so no line ever reaches the limit, and the operator reads
#: "Step 5" that is word for word "Step 3". Counting how many distinct lines
#: came back is what sees that shape.
_LOOP_DUPLICATE_LINES = 3


class _RepetitionGuard:
    """Trips when generated text starts restating itself.

    The cap above bounds a cycle, but 700 tokens of the same sentence still
    reaches the operator as an answer. This reports the generation as failed so
    the caller falls back to the grounded draft it already composed.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._duplicated = 0
        self._pending = ""

    def feed(self, delta: str) -> None:
        self._pending += delta
        *complete, self._pending = self._pending.split("\n")
        for line in complete:
            self._count(line)

    def finish(self) -> None:
        self._count(self._pending)
        self._pending = ""

    def _count(self, line: str) -> None:
        stripped = line.strip()
        if len(stripped) < _LOOP_MIN_LINE_CHARS:
            return
        seen = self._counts[stripped] = self._counts.get(stripped, 0) + 1
        if seen >= _LOOP_REPEAT_LIMIT:
            raise LLMProviderResponseError(
                "The model repeated the same line and the answer was discarded."
            )
        if seen == 2:
            self._duplicated += 1
            if self._duplicated >= _LOOP_DUPLICATE_LINES:
                raise LLMProviderResponseError(
                    "The model restated a whole block and the answer was discarded."
                )


def _urlopen_bytes(request: Request, timeout_seconds: int) -> bytes:
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def _urlopen_lines(request: Request, timeout_seconds: int) -> Iterator[bytes]:
    with urlopen(request, timeout=timeout_seconds) as response:
        for line in response:
            yield line


class LLMProvider(Protocol):
    name: str

    def synthesize(self, context: dict) -> str:
        """Return grounded final answer text."""
        ...

    def complete(self, system: str, user: str, *, max_tokens: int = 32) -> str:
        """Answer a short prompt of the caller's own.

        ``synthesize`` always loads the answer-synthesiser prompt and serialises
        the machine context into it, which is right for writing an answer and
        useless for anything else. The intent router needs to ask the model a
        different, much smaller question, and needs the reply to be the model's
        answer to *that* question rather than a rewritten machine summary.
        """
        ...

    def synthesize_stream(self, context: dict) -> Iterator[str]:
        """Yield provider-native answer deltas."""
        ...


class DeterministicLLMProvider:
    name = "deterministic"

    def synthesize(self, context: dict) -> str:
        return str(context.get("draft") or "")

    def complete(self, system: str, user: str, *, max_tokens: int = 32) -> str:
        # There is no model here to ask. An empty reply is how the caller is
        # told to keep whatever it would have done without one.
        return ""

    def synthesize_stream(self, context: dict) -> Iterator[str]:
        draft = self.synthesize(context)
        yield from _deterministic_chunks(draft)


class OllamaChatProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        transport: Transport = _urlopen_bytes,
        stream_transport: StreamTransport = _urlopen_lines,
        timeout_seconds: int = 45,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport
        self.stream_transport = stream_transport
        self.timeout_seconds = timeout_seconds

    def synthesize(self, context: dict) -> str:
        request = self._request(context, stream=False)
        try:
            response = json.loads(self.transport(request, self.timeout_seconds).decode("utf8"))
        except LLMProviderError:
            raise
        except Exception as exc:
            raise _safe_transport_error(self.name, exc) from exc
        content = response.get("message", {}).get("content") or response.get("response")
        text = _required_content(content, provider=self.name)
        guard = _RepetitionGuard()
        guard.feed(text)
        guard.finish()
        return text

    def synthesize_stream(self, context: dict) -> Iterator[str]:
        request = self._request(context, stream=True)
        emitted = False
        guard = _RepetitionGuard()
        try:
            for raw_line in self.stream_transport(request, self.timeout_seconds):
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LLMProviderResponseError(
                        "Ollama returned an invalid streaming response."
                    ) from exc
                if event.get("error"):
                    raise LLMProviderResponseError("Ollama streaming failed.")
                content = event.get("message", {}).get("content") or event.get("response")
                if isinstance(content, str) and content:
                    # Check before yielding: a delta that completes a repeated
                    # line must not reach the caller, because a token that has
                    # been sent cannot be taken back.
                    guard.feed(content)
                    emitted = True
                    yield content
            guard.finish()
        except LLMProviderError:
            raise
        except Exception as exc:
            raise _safe_transport_error(self.name, exc) from exc
        if not emitted:
            raise LLMProviderResponseError("Ollama returned an empty streaming response.")

    def complete(self, system: str, user: str, *, max_tokens: int = 32) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # A label, not prose: a tight cap keeps a chatty model from
            # explaining itself, and keeps this off the turn's critical path.
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            response = json.loads(self.transport(request, self.timeout_seconds).decode("utf8"))
        except LLMProviderError:
            raise
        except Exception as exc:
            raise _safe_transport_error(self.name, exc) from exc
        content = response.get("message", {}).get("content") or response.get("response")
        return str(content or "")

    def _request(self, context: dict, *, stream: bool) -> Request:
        payload = {
            "model": self.model,
            "stream": stream,
            "messages": _provider_messages(context),
            "options": dict(OLLAMA_GENERATION_OPTIONS),
        }
        return Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )


class AzureOpenAIChatProvider:
    name = "azure_openai"

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_key: str,
        api_version: str = "2024-10-21",
        transport: Transport = _urlopen_bytes,
        stream_transport: StreamTransport = _urlopen_lines,
        timeout_seconds: int = 45,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self.api_key = api_key
        self.api_version = api_version
        self.transport = transport
        self.stream_transport = stream_transport
        self.timeout_seconds = timeout_seconds

    def synthesize(self, context: dict) -> str:
        request = self._request(context, stream=False)
        try:
            response = json.loads(self.transport(request, self.timeout_seconds).decode("utf8"))
        except LLMProviderError:
            raise
        except Exception as exc:
            raise _safe_transport_error(self.name, exc) from exc
        content = response.get("choices", [{}])[0].get("message", {}).get("content")
        return _required_content(content, provider=self.name)

    def synthesize_stream(self, context: dict) -> Iterator[str]:
        request = self._request(context, stream=True)
        emitted = False
        try:
            for raw_line in self.stream_transport(request, self.timeout_seconds):
                line = raw_line.decode("utf-8").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line.removeprefix("data:").strip()
                if line == "[DONE]":
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LLMProviderResponseError(
                        "Azure OpenAI returned an invalid streaming response."
                    ) from exc
                if event.get("error"):
                    raise LLMProviderResponseError("Azure OpenAI streaming failed.")
                content = event.get("choices", [{}])[0].get("delta", {}).get("content")
                if isinstance(content, str) and content:
                    emitted = True
                    yield content
        except LLMProviderError:
            raise
        except Exception as exc:
            raise _safe_transport_error(self.name, exc) from exc
        if not emitted:
            raise LLMProviderResponseError("Azure OpenAI returned an empty streaming response.")

    def complete(self, system: str, user: str, *, max_tokens: int = 32) -> str:
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        request = Request(
            (
                f"{self.endpoint}/openai/deployments/{quote(self.deployment, safe='')}"
                f"/chat/completions?{urlencode({'api-version': self.api_version})}"
            ),
            data=json.dumps(payload).encode("utf8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-key": self.api_key,
            },
            method="POST",
        )
        try:
            response = json.loads(self.transport(request, self.timeout_seconds).decode("utf8"))
        except LLMProviderError:
            raise
        except Exception as exc:
            raise _safe_transport_error(self.name, exc) from exc
        choices = response.get("choices") or [{}]
        return str(choices[0].get("message", {}).get("content") or "")

    def _request(self, context: dict, *, stream: bool) -> Request:
        payload = {
            "messages": _provider_messages(context),
            "temperature": 0,
            "max_tokens": 700,
            "stream": stream,
        }
        return Request(
            (
                f"{self.endpoint}/openai/deployments/{quote(self.deployment, safe='')}"
                f"/chat/completions?{urlencode({'api-version': self.api_version})}"
            ),
            data=json.dumps(payload).encode("utf8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "api-key": self.api_key,
            },
            method="POST",
        )


def build_llm_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    provider = settings.llm_provider.strip().lower()

    if provider == "deterministic":
        return DeterministicLLMProvider()

    if provider == "ollama":
        if not settings.ollama_base_url.strip() or not settings.ollama_chat_model.strip():
            raise LLMProviderConfigurationError(
                "LLM_PROVIDER=ollama requires OLLAMA_BASE_URL and OLLAMA_CHAT_MODEL."
            )
        return OllamaChatProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_chat_model,
        )

    if provider == "azure_openai":
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", settings.azure_openai_endpoint),
                ("AZURE_OPENAI_DEPLOYMENT", settings.azure_openai_deployment),
                ("AZURE_OPENAI_API_KEY", settings.azure_openai_api_key),
            )
            if not value
        ]
        if missing:
            raise LLMProviderConfigurationError(
                "LLM_PROVIDER=azure_openai is missing required settings: " + ", ".join(missing)
            )
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-preview)?", settings.azure_openai_api_version):
            raise LLMProviderConfigurationError(
                "AZURE_OPENAI_API_VERSION must use YYYY-MM-DD or YYYY-MM-DD-preview format."
            )

        return AzureOpenAIChatProvider(
            endpoint=settings.azure_openai_endpoint,
            deployment=settings.azure_openai_deployment,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )

    raise LLMProviderConfigurationError(
        f"Unsupported LLM_PROVIDER {settings.llm_provider!r}. "
        "Use deterministic, ollama, or azure_openai."
    )


def stream_synthesis(provider: LLMProvider, context: dict) -> Iterator[str]:
    stream_method = getattr(provider, "synthesize_stream", None)
    if callable(stream_method):
        yield from stream_method(context)
        return
    content = provider.synthesize(context)
    if content:
        yield content


def provider_readiness(
    settings: Settings | None = None,
    *,
    timeout_seconds: float = 0.75,
) -> dict:
    active_settings = settings or get_settings()
    try:
        provider = build_llm_provider(active_settings)
    except LLMProviderConfigurationError as exc:
        return {
            "status": "unavailable",
            "provider": active_settings.llm_provider,
            "error": str(exc),
        }

    if provider.name == "deterministic":
        return {"status": "ready", "provider": provider.name, "mode": "test"}

    try:
        if provider.name == "ollama":
            api_request = Request(
                f"{active_settings.ollama_base_url.rstrip('/')}/api/tags",
                headers={"Accept": "application/json"},
            )
            with urlopen(api_request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            available_models = {
                str(item.get("name") or item.get("model"))
                for item in payload.get("models", [])
                if isinstance(item, dict)
            }
            configured_model = active_settings.ollama_chat_model
            if configured_model not in available_models:
                return {
                    "status": "unavailable",
                    "provider": provider.name,
                    "error": "Configured Ollama chat model is not installed.",
                }
        elif provider.name == "azure_openai":
            query = urlencode({"api-version": active_settings.azure_openai_api_version})
            api_request = Request(
                f"{active_settings.azure_openai_endpoint.rstrip('/')}/openai/models?{query}",
                headers={
                    "Accept": "application/json",
                    "api-key": active_settings.azure_openai_api_key,
                },
            )
            with urlopen(api_request, timeout=timeout_seconds) as response:
                json.loads(response.read().decode("utf-8"))
    except Exception:
        return {
            "status": "unavailable",
            "provider": provider.name,
            "error": "Configured LLM provider readiness check failed.",
        }
    return {"status": "ready", "provider": provider.name}


def _required_content(content: object, *, provider: str) -> str:
    if isinstance(content, str) and content.strip():
        return content.strip()
    raise LLMProviderResponseError(f"{provider} returned an empty synthesis response.")


def _deterministic_chunks(draft: str) -> Iterator[str]:
    if not draft:
        return
    sections = re.split(r"(\n\n+|(?<=[.!?])\s+)", draft)
    chunks = [chunk for chunk in sections if chunk]
    if len(chunks) == 1 and len(draft) > 1:
        split_at = max(1, len(draft) // 2)
        chunks = [draft[:split_at], draft[split_at:]]
    yield from chunks


def _safe_transport_error(provider: str, error: Exception) -> LLMProviderResponseError:
    if isinstance(error, HTTPError):
        return LLMProviderResponseError(f"{provider} request failed with HTTP status {error.code}.")
    if isinstance(error, (URLError, TimeoutError, OSError)):
        return LLMProviderResponseError(f"{provider} network request failed.")
    if isinstance(error, (UnicodeDecodeError, json.JSONDecodeError)):
        return LLMProviderResponseError(f"{provider} returned an invalid JSON response.")
    return LLMProviderResponseError(f"{provider} request failed.")


#: What the synthesizer is actually asked to work from.
#:
#: The answer context carries the whole turn because the API and the fallback
#: need it. The model does not: ``draft`` is a rendering of the sections that
#: are already here, ``agentTrace``/``intents``/``toolCalls`` are routing
#: internals, and ``telemetry``/``contract``/``orders``/``quotes``/
#: ``serviceHistory``/``diagnosticSteps``/``recommendedActions`` have already
#: been summarised into ``structuredAnswer`` by the composer.
#:
#: Sending all of it built a ~4,950-token prompt against a 4,096-token window,
#: so the server truncated it - dropping roughly 850 tokens of the retrieved
#: evidence silently, and spending the inference budget re-reading duplicates.
_MODEL_CONTEXT_KEYS = ("machineLabel", "userMessage", "structuredAnswer", "accessDenials")

#: Enough passage to rewrite from. The prompt asks for a summary in the model's
#: own words, not a copied extract, so the whole chunk is not needed.
_MAX_EVIDENCE_ITEMS = 3
_MAX_EXCERPT_CHARS = 240


def _model_context(context: dict) -> dict:
    payload = {key: context[key] for key in _MODEL_CONTEXT_KEYS if context.get(key)}
    evidence = context.get("evidence") or []
    if evidence:
        focus_query = " ".join(
            (
                str(context.get("userMessage") or ""),
                json.dumps(context.get("structuredAnswer") or {}, ensure_ascii=True),
            )
        )
        payload["evidence"] = [
            _trimmed_evidence(item, focus_query) for item in evidence[:_MAX_EVIDENCE_ITEMS]
        ]
    return payload


def _trimmed_evidence(item: dict, focus_query: str = "") -> dict:
    """A citation the model must keep, with only as much text as it needs."""
    excerpt = focus_alarm_passage(str(item.get("excerpt") or ""), focus_query)
    return {
        "title": item.get("title"),
        "page": item.get("page"),
        "sourceUri": item.get("sourceUri"),
        "excerpt": excerpt[:_MAX_EXCERPT_CHARS],
    }


def _provider_messages(context: dict) -> list[dict[str, str]]:
    """The system prompt comes from the registry, not from a literal here.

    ``/api/v1/prompts/{name}`` serves these files, so a reviewer reading the
    published prompt is reading the one the model was actually sent. While this
    text lived inline the endpoint published a different, shorter document.
    """
    return [
        {"role": "system", "content": load_prompt("answer_synthesizer")},
        {
            "role": "user",
            "content": json.dumps(_model_context(context), ensure_ascii=True, sort_keys=True),
        },
    ]
