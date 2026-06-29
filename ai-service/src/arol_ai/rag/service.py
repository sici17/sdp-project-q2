from arol_ai.config import Settings, get_settings
from arol_ai.graph.state import Evidence
from arol_ai.rag.evidence_quality import sanitize_manual_evidence
from arol_ai.rag.ingestion import _manuals_dir, load_manifest
from arol_ai.rag.models import ManualManifestEntry, ManualSearchScope
from arol_ai.rag.ollama import OllamaEmbeddingClient
from arol_ai.rag.qdrant_store import QdrantManualStore
from arol_ai.rag.retriever import ManualVectorRetriever


def build_manual_retriever(settings: Settings | None = None) -> ManualVectorRetriever:
    """The retriever behind the service's own manual-search endpoint.

    Deliberately no PDF-reading lexical leg. Adding one fixed the ranking but
    put a first-request parse of every manual into the serving path, which took
    the endpoint past the gateway's upstream timeout on a cold container. The
    ranking problem it was solving — an alarm code answered from whatever prose
    sits nearest in embedding space — is fixed in the reranker instead, which
    costs nothing, and in Doc MCP, which is what serves chat.
    """
    active_settings = settings or get_settings()
    return ManualVectorRetriever(
        embeddings=OllamaEmbeddingClient(
            base_url=active_settings.ollama_base_url,
            model=active_settings.ollama_embed_model,
        ),
        store=QdrantManualStore(
            url=active_settings.qdrant_url,
            collection_name=active_settings.doc_collection,
        ),
    )


def manual_index_status(
    settings: Settings | None = None,
    *,
    store: QdrantManualStore | None = None,
) -> dict:
    active_settings = settings or get_settings()
    manuals_dir = _manuals_dir(active_settings)
    manifest = load_manifest(manuals_dir)
    active_store = store or QdrantManualStore(
        url=active_settings.qdrant_url,
        collection_name=active_settings.doc_collection,
    )

    try:
        total_chunks = active_store.count_chunks()
        manuals = [
            {
                **entry.to_dict(),
                "pdfExists": entry.pdf_path(manuals_dir).exists(),
                "indexedChunkCount": active_store.count_chunks(
                    machine_id=entry.machine_id,
                    manual_version=entry.version,
                    language=entry.language,
                ),
            }
            for entry in manifest
        ]
    except Exception as exc:
        return {
            "ragEnabled": active_settings.doc_rag_enabled,
            "collection": active_settings.doc_collection,
            "embeddingModel": active_settings.ollama_embed_model,
            "status": "unavailable",
            "totalIndexedChunks": 0,
            "error": str(exc),
            "manuals": [
                {
                    **entry.to_dict(),
                    "pdfExists": entry.pdf_path(manuals_dir).exists(),
                    "indexedChunkCount": None,
                    "status": "unknown",
                }
                for entry in manifest
            ],
        }

    indexed_manuals = [
        {
            **manual,
            "status": (
                "indexed"
                if manual["pdfExists"] and manual["indexedChunkCount"] > 0
                else "unindexed"
            ),
        }
        for manual in manuals
    ]
    indexed_count = sum(manual["status"] == "indexed" for manual in indexed_manuals)
    expected_count = len(indexed_manuals)
    if expected_count > 0 and indexed_count == expected_count:
        overall_status = "indexed"
    elif total_chunks > 0:
        overall_status = "partial"
    else:
        overall_status = "empty"

    return {
        "ragEnabled": active_settings.doc_rag_enabled,
        "collection": active_settings.doc_collection,
        "embeddingModel": active_settings.ollama_embed_model,
        "status": overall_status,
        "totalIndexedChunks": total_chunks,
        "expectedManualCount": expected_count,
        "indexedManualCount": indexed_count,
        "unindexedManualIds": [
            manual["machineId"] for manual in indexed_manuals if manual["status"] != "indexed"
        ],
        "manuals": indexed_manuals,
    }


def search_manual_index(
    *,
    machine_id: str,
    query: str,
    manual_version: str | None = None,
    language: str | None = None,
    limit: int = 3,
    settings: Settings | None = None,
    retriever: ManualVectorRetriever | None = None,
) -> list[Evidence]:
    active_settings = settings or get_settings()
    manuals_dir = _manuals_dir(active_settings)
    manifest = load_manifest(manuals_dir)
    entry = next((item for item in manifest if item.machine_id == machine_id), None)
    if entry is None:
        raise ValueError("Machine not found.")

    base_machine_ids = {entry.machine_id}

    active_retriever = retriever or build_manual_retriever(active_settings)
    searchable_entries = _searchable_entries(
        manifest,
        base_machine_ids,
        manual_version=manual_version,
        language=language,
    )
    scopes = [
        ManualSearchScope(
            machine_id=item.machine_id,
            manual_version=item.version,
            language=item.language,
        )
        for item in searchable_entries
    ]

    return sanitize_manual_evidence(
        active_retriever.search_many(
            scopes=scopes,
            query=query,
            limit=limit,
        )
    )


def _searchable_entries(
    entries: list[ManualManifestEntry],
    machine_ids: set[str],
    *,
    manual_version: str | None = None,
    language: str | None = None,
) -> list[ManualManifestEntry]:
    return [
        entry
        for entry in entries
        if entry.machine_id in machine_ids or machine_ids.intersection(entry.related_machine_ids)
        if manual_version is None or entry.version == manual_version
        if language is None or entry.language == language
    ]
