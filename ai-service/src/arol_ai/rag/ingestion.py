import json
import os
from pathlib import Path
from typing import TypeVar

from arol_ai.config import Settings, get_settings
from arol_ai.rag.chunking import chunk_manual_pages
from arol_ai.rag.models import ManualManifestEntry
from arol_ai.rag.ollama import OllamaEmbeddingClient
from arol_ai.rag.pdf import extract_pdf_pages
from arol_ai.rag.qdrant_store import QdrantManualStore

DEFAULT_EMBED_BATCH_SIZE = 24
T = TypeVar("T")


def ingest_manuals(settings: Settings | None = None, *, reset: bool = False) -> int:
    """Index every manual that is not already indexed.

    Resumable on purpose. This runs on every start of the stack, takes about
    twelve minutes on a laptop, and used to reset the collection first — so a
    complete index was destroyed and rebuilt each time, and a failure anywhere
    in it threw away all the work before it. Now each manual is skipped when the
    collection already holds the chunks it produces, which makes a restart after
    a transient failure cost only the manual that failed.

    ``reset`` still wipes the collection first, for when the chunker or the
    embedding model changes and the stored vectors are no longer comparable.
    """
    active_settings = settings or get_settings()
    manuals_dir = _manuals_dir(active_settings)
    manifest = load_manifest(manuals_dir)
    batch_size = int(os.environ.get("DOC_INGEST_BATCH_SIZE", str(DEFAULT_EMBED_BATCH_SIZE)))
    embeddings = OllamaEmbeddingClient(
        base_url=active_settings.ollama_base_url,
        model=active_settings.ollama_embed_model,
    )
    store = QdrantManualStore(
        url=active_settings.qdrant_url,
        collection_name=active_settings.doc_collection,
    )

    if reset:
        print("Resetting the manual collection.", flush=True)
        store.reset_collection()

    total_chunks = 0
    skipped = 0
    # Progress goes to stdout because this runs as a one-shot container whose
    # only record is its log. Without it a failure shows a traceback and no
    # indication of which manual it reached, or whether it reached one at all.
    print(f"Indexing {len(manifest)} manual(s) from {manuals_dir}.", flush=True)
    for index, entry in enumerate(manifest, start=1):
        pages = extract_pdf_pages(entry.pdf_path(manuals_dir))
        chunks = chunk_manual_pages(entry, pages)
        label = f"[{index}/{len(manifest)}] {entry.machine_id}"

        if _already_indexed(store, entry, len(chunks)):
            print(f"{label}: {len(chunks)} chunk(s) already indexed, skipping.", flush=True)
            skipped += 1
            total_chunks += len(chunks)
            continue

        print(
            f"{label}: indexing {len(chunks)} chunk(s) from {entry.pdf_path(manuals_dir).name}",
            flush=True,
        )
        for chunk_batch in _batches(chunks, batch_size):
            vectors = embeddings.embed_texts([chunk.text for chunk in chunk_batch])
            store.upsert(chunk_batch, vectors)
        total_chunks += len(chunks)

    if skipped:
        print(f"{skipped} of {len(manifest)} manual(s) were already indexed.", flush=True)

    return total_chunks


def _already_indexed(store: QdrantManualStore, entry: ManualManifestEntry, expected: int) -> bool:
    """True when the collection already holds this manual's chunks.

    A count short of expected means a previous run stopped partway, so the
    manual is indexed again; upserts are keyed by chunk id, so redoing one is
    harmless.
    """
    try:
        return (
            store.count_chunks(
                machine_id=entry.machine_id,
                manual_version=entry.version,
                language=entry.language,
            )
            >= expected
        )
    except Exception:
        # An unreachable or empty collection is not a reason to fail here: the
        # indexing attempt that follows will surface the real problem.
        return False


def _batches(items: list[T], size: int) -> list[list[T]]:
    if size < 1:
        raise ValueError("Batch size must be greater than zero.")

    return [items[index : index + size] for index in range(0, len(items), size)]


def load_manifest(manuals_dir: Path) -> list[ManualManifestEntry]:
    manifest_path = _manifest_path(manuals_dir)
    with manifest_path.open("r", encoding="utf8") as file:
        payload = json.load(file)

    return [ManualManifestEntry.from_dict(item) for item in payload["manuals"]]


def _manifest_path(manuals_dir: Path) -> Path:
    """Locate the manual manifest.

    The manifest is generated from the fleet dataset, so that the machine to
    manual join by serial number cannot drift from the workbook. A manifest
    sitting beside the PDFs still wins, which keeps a self-contained manual
    directory usable for tests and for local corpora.
    """
    override = os.environ.get("MANUALS_MANIFEST_PATH")
    if override:
        return Path(override)

    local = manuals_dir / "manifest.json"
    if local.is_file():
        return local

    return _repo_root() / "data" / "manuals-manifest.json"


def _manuals_dir(settings: Settings) -> Path:
    if settings.manuals_dir:
        return settings.manuals_dir

    return _repo_root() / "requirements" / "manuals"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]
