import re

from arol_ai.rag.models import ExtractedPage, ManualChunk, ManualManifestEntry

DEFAULT_CHUNK_SIZE = 1200
DEFAULT_OVERLAP = 160


def chunk_manual_pages(
    manifest: ManualManifestEntry,
    pages: list[ExtractedPage],
    *,
    max_chars: int = DEFAULT_CHUNK_SIZE,
    overlap_chars: int = DEFAULT_OVERLAP,
) -> list[ManualChunk]:
    chunks: list[ManualChunk] = []

    for page in pages:
        text = _page_text(page)
        if not text:
            continue

        section = _detect_section(text)
        for chunk_text in _split_text(text, max_chars=max_chars, overlap_chars=overlap_chars):
            chunks.append(
                ManualChunk.create(
                    manifest=manifest,
                    section=section,
                    page_start=page.page,
                    page_end=page.page,
                    text=chunk_text,
                )
            )

    return chunks


def _page_text(page: ExtractedPage) -> str:
    parts = [page.text, *page.tables]
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _detect_section(text: str) -> str:
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue

        match = re.match(
            r"^(?:section|warning|procedure|troubleshooting):\s*(.+)$", normalized, re.I
        )
        if match:
            return match.group(1).strip()

        if len(normalized) <= 80 and normalized[:1].isupper():
            return normalized

    return "Manual"


def _split_text(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    start = 0

    while start < len(normalized):
        end = min(start + max_chars, len(normalized))
        if end < len(normalized):
            paragraph_break = normalized.rfind("\n\n", start, end)
            if paragraph_break > start + max_chars // 2:
                end = paragraph_break

        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(normalized):
            break

        start = max(0, end - overlap_chars)

    return chunks
