from qdrant_client import QdrantClient, models

from arol_ai.graph.state import Evidence
from arol_ai.rag.models import ManualChunk
from arol_ai.rag.scoring import (
    ALARM_QUERY_TERMS,
    MIN_LEXICAL_SCORE,
    PROCEDURAL_QUERY_TERMS,
    SAFETY_QUERY_TERMS,
    error_code_score,
)
from arol_ai.rag.text import (
    clean_manual_text,
    expanded_search_tokens,
    normalize_for_search,
    ordered_search_terms,
)


class QdrantManualStore:
    def __init__(self, *, url: str, collection_name: str) -> None:
        self.collection_name = collection_name
        self.client = QdrantClient(url=url, check_compatibility=False)

    def reset_collection(self) -> None:
        if self.client.collection_exists(self.collection_name):
            self.client.delete_collection(self.collection_name)

    def ensure_collection(self, vector_size: int) -> None:
        if self.client.collection_exists(self.collection_name):
            return

        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        )

    def upsert(self, chunks: list[ManualChunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("Chunk and vector counts must match.")

        if not chunks:
            return

        self.ensure_collection(len(vectors[0]))
        points = [
            models.PointStruct(id=chunk.id, vector=vector, payload=chunk.payload())
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self.client.upsert(collection_name=self.collection_name, points=points)

    def search(
        self,
        *,
        query_vector: list[float],
        machine_id: str,
        manual_version: str | None,
        language: str | None,
        limit: int,
    ) -> list[Evidence]:
        query_filter = _manual_filter(machine_id, manual_version, language)

        result = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        points = getattr(result, "points", result)

        return [_point_to_evidence(point) for point in points]

    def lexical_search(
        self,
        *,
        machine_id: str,
        manual_version: str | None,
        language: str | None,
        query: str,
        limit: int,
    ) -> list[Evidence]:
        if not self.client.collection_exists(self.collection_name):
            return []

        query_filter = _manual_filter(machine_id, manual_version, language)
        query_text = normalize_for_search(query)
        query_phrase = " ".join(ordered_search_terms(query))
        query_terms = expanded_search_tokens(query)
        if not query_terms:
            return []

        points = []
        offset = None
        while True:
            page, offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=query_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points.extend(page)
            if offset is None:
                break

        scored = []
        for point in points:
            evidence = _point_to_evidence(point)
            score = _lexical_score(query_text, query_phrase, query_terms, evidence)
            if score >= MIN_LEXICAL_SCORE:
                confidence = min(0.99, 0.72 + score / 200)
                scored.append(
                    (
                        score,
                        Evidence(
                            source=evidence.source,
                            title=evidence.title,
                            excerpt=evidence.excerpt,
                            page=evidence.page,
                            confidence=round(confidence, 3),
                            manual_version=evidence.manual_version,
                            language=evidence.language,
                            source_uri=evidence.source_uri,
                            chunk_id=evidence.chunk_id,
                            section=evidence.section,
                            score=round(confidence, 3),
                            chunk_kind=evidence.chunk_kind,
                            topics=evidence.topics,
                            alarm_codes=evidence.alarm_codes,
                            safety_level=evidence.safety_level,
                        ),
                    )
                )

        scored.sort(key=lambda item: (item[0], item[1].page or 0), reverse=True)
        return [item for _, item in scored[:limit]]

    def count_chunks(
        self,
        *,
        machine_id: str | None = None,
        manual_version: str | None = None,
        language: str | None = None,
    ) -> int:
        if not self.client.collection_exists(self.collection_name):
            return 0

        count_filter = _manual_filter(machine_id, manual_version, language) if machine_id else None
        result = self.client.count(
            collection_name=self.collection_name,
            count_filter=count_filter,
            exact=True,
        )

        return int(result.count)


def _manual_filter(
    machine_id: str | None,
    manual_version: str | None,
    language: str | None,
) -> models.Filter:
    conditions: list[models.FieldCondition] = []

    if machine_id:
        conditions.append(
            models.FieldCondition(key="machineId", match=models.MatchValue(value=machine_id))
        )

    if manual_version:
        conditions.append(
            models.FieldCondition(
                key="manualVersion", match=models.MatchValue(value=manual_version)
            )
        )

    if language:
        conditions.append(
            models.FieldCondition(key="language", match=models.MatchValue(value=language))
        )

    return models.Filter(must=conditions)


def _point_to_evidence(point) -> Evidence:
    payload = point.payload or {}
    raw_score = getattr(point, "score", None)
    score = float(raw_score) if raw_score is not None else None
    page = payload.get("pageStart")

    return Evidence(
        source="manual",
        title=payload.get("section") or payload.get("title") or "Manual citation",
        excerpt=clean_manual_text(payload.get("text", "")),
        page=int(page) if page is not None else None,
        confidence=_confidence(score),
        manual_version=payload.get("manualVersion"),
        language=payload.get("language"),
        source_uri=payload.get("sourceUri"),
        chunk_id=payload.get("chunkId"),
        section=payload.get("section"),
        score=score,
        chunk_kind=payload.get("chunkKind"),
        topics=tuple(payload.get("topics") or ()),
        alarm_codes=tuple(payload.get("alarmCodes") or ()),
        safety_level=payload.get("safetyLevel"),
    )


def _confidence(score: float | None) -> float | None:
    if score is None:
        return None

    return max(0.0, min(0.99, score))


def _lexical_score(
    query_text: str,
    query_phrase: str,
    query_terms: set[str],
    evidence: Evidence,
) -> int:
    text = normalize_for_search(evidence.excerpt)
    title = normalize_for_search(
        " ".join(part for part in [evidence.title, evidence.section or ""] if part)
    )
    text_terms = set(text.split())
    title_terms = set(title.split())
    score = 0

    if query_text and query_text in text:
        score += 80
    if query_phrase and query_phrase in text:
        score += 80
    if title and title in query_text:
        score += 50
    if query_phrase and title and title in query_phrase:
        score += 50

    if query_text and query_text in f"{title} {text}":
        score += 35

    score += error_code_score(query_text=query_text, source_text=f"{title} {text}")
    score += _metadata_score(query_text=query_text, query_terms=query_terms, evidence=evidence)

    overlap = query_terms.intersection(text_terms)
    title_overlap = query_terms.intersection(title_terms)
    score += len(overlap)
    score += 6 * len(title_overlap)

    if len(query_terms) >= 3 and query_terms.issubset(text_terms):
        score += 20

    if "...." in evidence.excerpt:
        score -= 40

    return score


def _metadata_score(*, query_text: str, query_terms: set[str], evidence: Evidence) -> int:
    score = 0
    topics = set(evidence.topics)
    topic_overlap = query_terms.intersection(topics)
    score += 10 * len(topic_overlap)

    if topics and topic_overlap == topics and len(topics) >= 2:
        score += 12

    if evidence.chunk_kind in {"procedure", "troubleshooting"} and query_terms.intersection(
        ALARM_QUERY_TERMS.union(PROCEDURAL_QUERY_TERMS)
    ):
        score += 12

    if evidence.chunk_kind == "safety" and query_terms.intersection(SAFETY_QUERY_TERMS):
        score += 14

    if evidence.safety_level in {"technician", "safety-critical"} and query_terms.intersection(
        SAFETY_QUERY_TERMS
    ):
        score += 12

    code_texts = {normalize_for_search(code) for code in evidence.alarm_codes}
    exact_code_hits = {code for code in code_texts if code and code in query_text}
    score += 40 * len(exact_code_hits)

    code_terms = {term for code in code_texts for term in code.split() if term}
    if code_terms.intersection(query_terms) and query_terms.intersection(ALARM_QUERY_TERMS):
        score += 18

    return score
