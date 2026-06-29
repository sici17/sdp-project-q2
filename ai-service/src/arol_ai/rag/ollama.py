import json
import os
import time
from collections.abc import Callable
from urllib import error, request

Transport = Callable[[request.Request, float], bytes]

#: Generous on purpose. Embedding a batch of chunks costs whatever the machine
#: costs, and the first call also loads the model: on a developer laptop that
#: cold call took 23 seconds, so a 60-second ceiling left very little room on a
#: shared CI runner with nine other containers competing for the CPU.
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_EMBED_TIMEOUT_SECONDS", "180"))

#: Ingestion is a long one-shot job - eight manuals, some seventy batches - and
#: losing all of it to one slow HTTP call is a poor trade. Transient failures
#: are retried; a wrong model name or a malformed response still fails at once.
DEFAULT_MAX_ATTEMPTS = 3


class OllamaEmbeddingError(RuntimeError):
    """Embedding failed after exhausting retries."""


class OllamaEmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.transport = transport or _urlopen_transport
        self.sleep = sleep

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        payload = json.dumps({"model": self.model, "input": texts}).encode("utf8")
        raw = self._post_with_retry(payload)
        response = json.loads(raw.decode("utf8"))
        embeddings = response.get("embeddings")

        if embeddings is None and "embedding" in response:
            embeddings = [response["embedding"]]

        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ValueError("Ollama did not return one embedding per input text.")

        return embeddings

    def _post_with_retry(self, payload: bytes) -> bytes:
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            api_request = request.Request(
                f"{self.base_url}/api/embed",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                return self.transport(api_request, self.timeout_seconds)
            except error.HTTPError as exc:
                # A 4xx is our mistake - a missing model, a bad request - and
                # will fail identically however many times it is repeated.
                if exc.code < 500:
                    raise
                last_error = exc
            except (error.URLError, TimeoutError, OSError) as exc:
                last_error = exc

            if attempt < self.max_attempts:
                self.sleep(2.0 * attempt)

        raise OllamaEmbeddingError(
            f"Embedding request to {self.base_url} failed after {self.max_attempts} "
            f"attempts ({self.timeout_seconds:.0f}s each): {last_error}"
        ) from last_error


def _urlopen_transport(api_request: request.Request, timeout_seconds: float) -> bytes:
    with request.urlopen(api_request, timeout=timeout_seconds) as response:
        return response.read()
