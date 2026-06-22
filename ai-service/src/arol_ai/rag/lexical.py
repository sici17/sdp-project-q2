from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from arol_ai.graph.state import Evidence
from arol_ai.rag.chunking import chunk_manual_pages
from arol_ai.rag.evidence_quality import is_substantive_manual_text
from arol_ai.rag.ingestion import load_manifest
from arol_ai.rag.models import ManualChunk
from arol_ai.rag.pdf import extract_pdf_pages
from arol_ai.rag.scoring import (
    ALARM_QUERY_TERMS,
    MIN_LEXICAL_SCORE,
    PROCEDURAL_QUERY_TERMS,
    SAFETY_QUERY_TERMS,
    error_code_score,
)
from arol_ai.rag.text import (
    alarm_code_phrases,
    clean_manual_text,
    expanded_search_tokens,
    normalize_for_search,
    ordered_search_terms,
)


@dataclass(frozen=True)
class ManualLexicalRetriever:
    manuals_dir: Path

    def search(
        self,
        *,
        machine_id: str,
        query: str,
        manual_version: str | None = None,
        language: str | None = None,
        limit: int = 3,
    ) -> list[Evidence]:
        query_text = normalize_for_search(query)
        query_phrase = " ".join(ordered_search_terms(query))
        query_terms = expanded_search_tokens(query)
        # An alarm code carries its own description; the manuals are written in
        # those words, not in the code.
        alarm_phrases = alarm_code_phrases(query)
        if not query_terms:
            return []

        scored: list[tuple[int, ManualChunk]] = []
        for chunk in _chunks_for_manual(str(self.manuals_dir), machine_id):
            if manual_version and chunk.manual_version != manual_version:
                continue
            if language and chunk.language != language:
                continue
            # Contents pages and publisher boilerplate match a query's
            # vocabulary without supporting an answer. Excluded here rather
            # than after ranking, so they cannot spend the result budget.
            if not is_substantive_manual_text(chunk.text, label=chunk.section):
                continue

            score = _chunk_score(query_text, query_phrase, query_terms, chunk, alarm_phrases)
            if score >= MIN_LEXICAL_SCORE:
                scored.append((score, chunk))

        scored.sort(key=lambda item: (item[0], -item[1].page_start), reverse=True)
        return [_chunk_to_evidence(chunk, score) for score, chunk in scored[:limit]]


@lru_cache(maxsize=16)
def _chunks_for_manual(manuals_dir: str, machine_id: str) -> tuple[ManualChunk, ...]:
    root = Path(manuals_dir)
    entry = next((item for item in load_manifest(root) if item.machine_id == machine_id), None)
    if entry is None:
        return ()

    pages = extract_pdf_pages(entry.pdf_path(root))
    return tuple(chunk_manual_pages(entry, pages))


def _chunk_score(
    query_text: str,
    query_phrase: str,
    query_terms: set[str],
    chunk: ManualChunk,
    alarm_phrases: tuple[str, ...] = (),
) -> int:
    text = normalize_for_search(chunk.text)
    section = normalize_for_search(chunk.section)
    text_terms = set(text.split())
    score = 0

    score += _alarm_phrase_score(alarm_phrases, text=text, section=section)

    if query_text and query_text in text:
        score += 80

    if query_phrase and query_phrase in text:
        score += 80

    if section and section in query_text:
        score += 50

    if query_phrase and section and section in query_phrase:
        score += 50

    if query_text and query_text in f"{section} {text}":
        score += 35

    score += error_code_score(query_text=query_text, source_text=f"{section} {text}")
    score += _metadata_score(
        query_text=query_text,
        query_terms=query_terms,
        chunk=chunk,
        alarm_phrases=alarm_phrases,
    )

    overlap = query_terms.intersection(text_terms)
    section_overlap = query_terms.intersection(set(section.split()))
    score += len(overlap)
    score += 6 * len(section_overlap)

    if len(query_terms) >= 3 and query_terms.issubset(text_terms):
        score += 20

    if _looks_like_toc(chunk.text):
        score -= 40

    return score


def _metadata_score(
    *,
    query_text: str,
    query_terms: set[str],
    chunk: ManualChunk,
    alarm_phrases: tuple[str, ...] = (),
) -> int:
    score = 0
    # Asking about a code is asking about a fault, even when the query contains
    # none of the words the vocabulary lists.
    asks_about_alarm = bool(alarm_phrases) or bool(query_terms.intersection(ALARM_QUERY_TERMS))
    topics = set(chunk.topics)
    topic_overlap = query_terms.intersection(topics)
    score += 10 * len(topic_overlap)

    if topics and topic_overlap == topics and len(topics) >= 2:
        score += 12

    if chunk.chunk_kind in {"procedure", "troubleshooting"} and (
        asks_about_alarm or query_terms.intersection(PROCEDURAL_QUERY_TERMS)
    ):
        score += 12

    # The fault tables are where a manual explains a condition and its remedy,
    # so they outrank a procedure that merely mentions the same component.
    if asks_about_alarm and chunk.chunk_kind in {"troubleshooting", "table"}:
        score += 30

    if chunk.chunk_kind == "safety" and query_terms.intersection(SAFETY_QUERY_TERMS):
        score += 14

    if chunk.safety_level in {"technician", "safety-critical"} and query_terms.intersection(
        SAFETY_QUERY_TERMS
    ):
        score += 12

    code_texts = {normalize_for_search(code) for code in chunk.alarm_codes}
    exact_code_hits = {code for code in code_texts if code and code in query_text}
    score += 40 * len(exact_code_hits)

    code_terms = {term for code in code_texts for term in code.split() if term}
    if code_terms.intersection(query_terms) and asks_about_alarm:
        score += 18

    return score


def _looks_like_toc(value: str) -> bool:
    dot_lines = sum(1 for line in value.splitlines() if "...." in line)
    return dot_lines >= 4


def _chunk_to_evidence(chunk: ManualChunk, lexical_score: int) -> Evidence:
    confidence = min(0.99, 0.72 + lexical_score / 200)
    return Evidence(
        source="manual",
        title=chunk.section or chunk.title,
        excerpt=clean_manual_text(chunk.text),
        page=chunk.page_start,
        confidence=round(confidence, 3),
        manual_version=chunk.manual_version,
        language=chunk.language,
        source_uri=chunk.source_uri,
        chunk_id=chunk.id,
        section=chunk.section,
        score=round(confidence, 3),
        chunk_kind=chunk.chunk_kind,
        topics=chunk.topics,
        alarm_codes=chunk.alarm_codes,
        safety_level=chunk.safety_level,
    )


def _alarm_phrase_score(alarm_phrases: tuple[str, ...], *, text: str, section: str) -> int:
    """Score a chunk against the description carried by an alarm code.

    The dataset's codes never appear in the manuals, so matching the mnemonic
    is the only way an alarm question reaches the passage that explains the
    condition. A chunk containing the whole phrase is a strong hit; partial
    word coverage is a weaker one, and is what distinguishes a relevant section
    from a page that merely shares one common word.
    """
    if not alarm_phrases:
        return 0

    score = 0
    haystack = f"{section} {text}"
    for phrase in alarm_phrases:
        if phrase in haystack:
            score += 70
            continue

        terms = [term for term in phrase.split() if len(term) > 2]
        if not terms:
            continue
        present = sum(1 for term in terms if term in haystack)
        if present == len(terms):
            score += 35
        elif present:
            score += int(20 * present / len(terms))
    return score
