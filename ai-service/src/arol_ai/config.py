import datetime as dt
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# The supplied fleet dataset covers a fixed window that ends 2026-08-04T23:00.
# Reasoning about open items, overdue work and expiry dates therefore uses this
# frozen reference date rather than the wall clock, so answers stay correct and
# reproducible however long after delivery the platform is run.
DEFAULT_PLATFORM_TODAY = "2026-08-05"


@dataclass(frozen=True)
class Settings:
    cors_origin: str | None
    default_machine_id: str
    doc_rag_enabled: bool
    qdrant_url: str
    ollama_base_url: str
    ollama_embed_model: str
    doc_collection: str
    manuals_dir: Path | None
    telemetry_service_url: str | None
    telemetry_stale_seconds: int
    session_ttl_seconds: int
    ai_service_shared_secret: str | None
    llm_provider: str
    ollama_chat_model: str
    azure_openai_endpoint: str | None
    azure_openai_deployment: str | None
    azure_openai_api_key: str | None
    ai_service_shared_secrets: tuple[str, ...] = ()
    platform_today: str = DEFAULT_PLATFORM_TODAY
    business_mcp_url: str | None = None
    doc_mcp_url: str | None = None
    mcp_shared_secret: str | None = None
    azure_openai_api_version: str = "2024-10-21"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    manuals_dir = os.environ.get("MANUALS_DIR")
    telemetry_service_url = os.environ.get("TELEMETRY_SERVICE_URL")

    shared_secrets = _shared_secrets(
        os.environ.get("AI_SERVICE_SHARED_SECRETS"),
        os.environ.get("AI_SERVICE_SHARED_SECRET"),
    )

    return Settings(
        cors_origin=os.environ.get("CORS_ORIGIN"),
        default_machine_id=os.environ.get("DEFAULT_MACHINE_ID", "MCH-0004"),
        doc_rag_enabled=_truthy(os.environ.get("DOC_RAG_ENABLED")),
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_embed_model=os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        doc_collection=os.environ.get("DOC_COLLECTION", "manual_chunks"),
        manuals_dir=Path(manuals_dir) if manuals_dir else None,
        telemetry_service_url=telemetry_service_url.rstrip("/") if telemetry_service_url else None,
        telemetry_stale_seconds=int(os.environ.get("TELEMETRY_STALE_SECONDS", "900")),
        session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "28800")),
        ai_service_shared_secret=shared_secrets[0] if shared_secrets else None,
        llm_provider=os.environ.get("LLM_PROVIDER", "deterministic"),
        ollama_chat_model=os.environ.get("OLLAMA_CHAT_MODEL", "qwen2.5:0.5b-instruct"),
        azure_openai_endpoint=_blank_to_none(os.environ.get("AZURE_OPENAI_ENDPOINT")),
        azure_openai_deployment=_blank_to_none(os.environ.get("AZURE_OPENAI_DEPLOYMENT")),
        azure_openai_api_key=_blank_to_none(os.environ.get("AZURE_OPENAI_API_KEY")),
        ai_service_shared_secrets=shared_secrets,
        platform_today=os.environ.get("PLATFORM_TODAY", DEFAULT_PLATFORM_TODAY),
        business_mcp_url=_normalized_url(os.environ.get("BUSINESS_MCP_URL")),
        doc_mcp_url=_normalized_url(os.environ.get("DOC_MCP_URL")),
        mcp_shared_secret=_blank_to_none(os.environ.get("MCP_SHARED_SECRET")),
        azure_openai_api_version=os.environ.get(
            "AZURE_OPENAI_API_VERSION",
            "2024-10-21",
        ),
    )


def _truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _blank_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None

    return value.strip()


def _shared_secrets(primary: str | None, fallback: str | None) -> tuple[str, ...]:
    source = primary if primary and primary.strip() else fallback
    if not source:
        return ()

    return tuple(item.strip() for item in source.split(",") if item.strip())


def _normalized_url(value: str | None) -> str | None:
    normalized = _blank_to_none(value)
    return normalized.rstrip("/") if normalized else None


def platform_today() -> dt.date:
    """The reference 'today' for all date reasoning in the platform.

    Every comparison against "now" that concerns dataset records must use this
    rather than :func:`datetime.date.today`, so that overdue work, quote expiry
    and service due dates stay consistent with the supplied data.
    """
    try:
        return dt.date.fromisoformat(get_settings().platform_today)
    except ValueError:
        return dt.date.fromisoformat(DEFAULT_PLATFORM_TODAY)


def platform_now() -> dt.datetime:
    """The reference instant, used for telemetry freshness."""
    return dt.datetime.combine(platform_today(), dt.time.min, tzinfo=dt.timezone.utc)
