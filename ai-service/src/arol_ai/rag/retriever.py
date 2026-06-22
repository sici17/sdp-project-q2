from arol_ai.graph.state import Evidence
from arol_ai.rag.lexical import ManualLexicalRetriever
from arol_ai.rag.models import ManualSearchScope
from arol_ai.rag.ollama import OllamaEmbeddingClient
from arol_ai.rag.qdrant_store import QdrantManualStore
from arol_ai.rag.scoring import (
    ALARM_QUERY_TERMS,
    PROCEDURAL_QUERY_TERMS,
    SAFETY_QUERY_TERMS,
    error_code_score,
)
from arol_ai.rag.text import (
    alarm_code_phrases,
    expanded_search_tokens,
    extract_code_tokens,
    normalize_for_search,
    search_tokens,
)


class ManualVectorRetriever:
    def __init__(
        self,
        *,
        embeddings: OllamaEmbeddingClient,
        store: QdrantManualStore,
        lexical: ManualLexicalRetriever | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.store = store
        self.lexical = lexical

    def search(
        self,
        *,
        machine_id: str,
        query: str,
        manual_version: str | None = None,
        language: str | None = None,
        limit: int = 3,
    ) -> list[Evidence]:
        lexical_evidence = []
        if hasattr(self.store, "lexical_search"):
            lexical_evidence = self.store.lexical_search(
                machine_id=machine_id,
                query=query,
                manual_version=manual_version,
                language=language,
                limit=max(limit, 6),
            )
        if self.lexical is not None:
            lexical_evidence.extend(
                self.lexical.search(
                    machine_id=machine_id,
                    query=query,
                    manual_version=manual_version,
                    language=language,
                    limit=max(limit, 6),
                )
            )

        ranked_lexical = _rerank_evidence(query, _dedupe_evidence(lexical_evidence))
        if ranked_lexical and _is_high_confidence_lexical_hit(query, ranked_lexical[0]):
            return ranked_lexical[:limit]

        query_vector = self.embeddings.embed_texts([query])[0]
        search_limit = max(limit, min(limit * 4, 12))
        vector_evidence = self.store.search(
            query_vector=query_vector,
            machine_id=machine_id,
            manual_version=manual_version,
            language=language,
            limit=search_limit,
        )
        evidence = _dedupe_evidence([*lexical_evidence, *vector_evidence])
        return _rerank_evidence(query, evidence)[:limit]

    def search_many(
        self,
        *,
        scopes: list[ManualSearchScope],
        query: str,
        limit: int = 3,
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        for scope in scopes:
            evidence.extend(
                self.search(
                    machine_id=scope.machine_id,
                    query=query,
                    manual_version=scope.manual_version,
                    language=scope.language,
                    limit=limit,
                )
            )

        return _rerank_evidence(query, _dedupe_evidence(evidence))[:limit]


def _rerank_evidence(query: str, evidence: list[Evidence]) -> list[Evidence]:
    query_terms = _expand_query_terms(_tokens(query))
    query_text = normalize_for_search(query)
    # Computed from the raw query: normalising strips the code apart.
    alarm_phrases = alarm_code_phrases(query)

    if not query_terms:
        return evidence

    return sorted(
        evidence,
        key=lambda item: (
            _lexical_score(query_terms, query_text, item, alarm_phrases),
            item.score or 0.0,
        ),
        reverse=True,
    )


def _alarm_phrase_score(alarm_phrases: tuple[str, ...], evidence: Evidence) -> int:
    """Rank an alarm question by the words the manual describes the alarm in.

    The dataset names an alarm ``ALnnn_MNEMONIC``. The manuals contain neither
    the code nor, usually, the mnemonic verbatim: they describe the condition in
    a fault table, in their own words. So the mnemonic's words are the real
    query, and a passage containing none of them is not about this alarm however
    similar it looks in embedding space.

    Without this, ``AL017_LOW_AIR_PRESSURE`` on a real manual returned roller
    height adjustment — fluent, plausible, and about something else entirely.
    """
    if not alarm_phrases:
        return 0

    source = normalize_for_search(
        " ".join(part for part in (evidence.title, evidence.section, evidence.excerpt) if part)
    )
    words = {word for phrase in alarm_phrases for word in phrase.split()}
    matched = sum(1 for word in words if word in source)

    score = 6 * matched
    if any(phrase in source for phrase in alarm_phrases):
        score += 30
    if evidence.chunk_kind in {"troubleshooting", "table"}:
        score += 12
    if not matched:
        score -= 25
    return score


def _lexical_score(
    query_terms: set[str],
    query_text: str,
    evidence: Evidence,
    alarm_phrases: tuple[str, ...] = (),
) -> int:
    title_terms = search_tokens(
        " ".join(part for part in [evidence.title, evidence.section or ""] if part)
    )
    excerpt_terms = search_tokens(evidence.excerpt)
    source_terms = title_terms.union(excerpt_terms)
    score = 3 * len(query_terms.intersection(title_terms))
    score += len(query_terms.intersection(excerpt_terms))
    normalized_title = normalize_for_search(
        " ".join(part for part in [evidence.title, evidence.section or ""] if part)
    )
    normalized_excerpt = normalize_for_search(evidence.excerpt)
    normalized_source = normalize_for_search(f"{normalized_title} {evidence.excerpt}")

    if normalized_title and (normalized_title in query_text or query_text in normalized_title):
        score += 20

    if query_text and query_text in normalized_source:
        score += 35

    score += error_code_score(query_text=query_text, source_text=normalized_source)
    score += _metadata_score(query_terms=query_terms, query_text=query_text, evidence=evidence)
    score += _alarm_phrase_score(alarm_phrases, evidence)

    if _looks_like_toc_or_boilerplate(evidence):
        score -= 10

    if {"check", "inspect", "verify", "confirm"}.intersection(query_terms) and {
        "inspect",
        "verify",
        "confirm",
        "check",
    }.intersection(excerpt_terms):
        score += 4

    if "daily" in query_terms and {"daily", "inspection", "operator"}.intersection(title_terms):
        score += 4

    if "escalation" not in query_terms and "escalation" in title_terms:
        score -= 3

    if "torque" in query_terms:
        if "torque" in source_terms:
            score += 18
        else:
            score -= 8

    if "alarm" in query_terms and {"symbol", "message"}.intersection(title_terms):
        score -= 5

    if {"torque", "alarm"}.issubset(query_terms) and {"adjustment", "head"}.intersection(
        source_terms
    ):
        score += 6

    if {"foil", "check", "intervention"}.issubset(query_terms) and {
        "foil",
        "presence",
        "device",
    }.intersection(source_terms):
        score += 18

    if {"main", "machine", "controls"}.intersection(query_terms) and {
        "closing",
        "controls",
        "icons",
        "operator",
        "panel",
    }.intersection(source_terms):
        score += 12

    if {"gripper", "pressure"}.issubset(query_terms) and {
        "closure",
        "gripper",
        "adjustment",
        "cap",
    }.intersection(source_terms):
        score += 14

    if "configuration" in query_terms and {"recipe", "parameters", "setup"}.intersection(
        source_terms
    ):
        score += 12

    if "manual is property" in normalized_excerpt:
        score -= 20

    return score


def _metadata_score(*, query_terms: set[str], query_text: str, evidence: Evidence) -> int:
    score = 0
    topics = set(evidence.topics)
    topic_overlap = query_terms.intersection(topics)
    score += 8 * len(topic_overlap)

    if topics and topic_overlap == topics and len(topics) >= 2:
        score += 10

    if evidence.chunk_kind in {"procedure", "troubleshooting"} and query_terms.intersection(
        ALARM_QUERY_TERMS.union(PROCEDURAL_QUERY_TERMS)
    ):
        score += 10

    if evidence.chunk_kind == "safety" and query_terms.intersection(SAFETY_QUERY_TERMS):
        score += 12

    if evidence.safety_level in {"technician", "safety-critical"} and query_terms.intersection(
        SAFETY_QUERY_TERMS
    ):
        score += 10

    code_texts = {normalize_for_search(code) for code in evidence.alarm_codes}
    exact_code_hits = {code for code in code_texts if code and code in query_text}
    score += 35 * len(exact_code_hits)

    code_terms = {term for code in code_texts for term in code.split() if term}
    if code_terms.intersection(query_terms) and query_terms.intersection(ALARM_QUERY_TERMS):
        score += 14

    return score


def _tokens(value: str) -> set[str]:
    return expanded_search_tokens(value)


def _expand_query_terms(terms: set[str]) -> set[str]:
    expanded = set(terms)

    if "check" in terms:
        expanded.update({"inspect", "verify", "confirm"})

    if "inspection" in terms or "daily" in terms:
        expanded.update({"operator", "guards", "lubrication", "interlocks"})

    if "alarm" in terms:
        expanded.update({"troubleshooting", "torque_high"})

    if "torque" in terms:
        expanded.update({"adjustment", "head", "screwing", "cap"})

    return expanded


def _is_high_confidence_lexical_hit(query: str, evidence: Evidence) -> bool:
    if (evidence.score or evidence.confidence or 0.0) >= 0.9:
        return True

    query_text = normalize_for_search(query)
    source_text = normalize_for_search(
        f"{evidence.title} {evidence.section or ''} {evidence.excerpt}"
    )
    query_codes = extract_code_tokens(query_text)
    if query_codes and query_codes.issubset(extract_code_tokens(source_text)):
        return True

    evidence_codes = {normalize_for_search(code) for code in evidence.alarm_codes}
    if evidence_codes and any(code in query_text for code in evidence_codes):
        return True

    return bool(query_text and query_text in source_text)


def _dedupe_evidence(evidence: list[Evidence]) -> list[Evidence]:
    deduped: dict[str, Evidence] = {}
    for item in evidence:
        key = item.chunk_id or f"{item.source_uri}:{item.page}:{item.section}:{item.excerpt[:40]}"
        existing = deduped.get(key)
        if existing is None or (item.score or 0.0) > (existing.score or 0.0):
            deduped[key] = item

    return list(deduped.values())


def _looks_like_toc_or_boilerplate(evidence: Evidence) -> bool:
    excerpt = evidence.excerpt.lower()
    if "...." in excerpt:
        return True

    stripped = excerpt.strip()
    return len(stripped) < 120 and "manual is property" in stripped
