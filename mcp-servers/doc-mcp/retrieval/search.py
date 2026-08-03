import json
import os
import re
from urllib import request

MIN_LEXICAL_SCORE = 8
ALARM_QUERY_TERMS = {
    "alarm",
    "alarms",
    "code",
    "error",
    "fault",
    "message",
    "troubleshooting",
}
PROCEDURAL_QUERY_TERMS = {
    "adjust",
    "check",
    "confirm",
    "inspect",
    "procedure",
    "verify",
    "what",
}
SAFETY_QUERY_TERMS = {
    "door",
    "emergency",
    "guard",
    "interlock",
    "safety",
    "stop",
    "voltage",
}
STOP_WORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "the",
    "this",
    "through",
    "to",
    "walk",
    "what",
    "with",
}
TERM_EXPANSIONS = {
    "alarm": ("error", "message", "troubleshooting"),
    "alarms": ("error", "message", "troubleshooting"),
    "code": ("error", "alarm", "message"),
    "configuration": ("parameters", "recipe", "setup"),
    "controls": ("icons", "operator", "panel"),
    "daily": ("inspection", "operator", "guards", "lubrication", "interlocks"),
    "error": ("alarm", "message"),
    "foil": ("presence", "check", "device"),
    "gripper": ("closure", "pressure", "adjustment"),
    "inspection": ("daily", "operator", "guards", "lubrication", "interlocks"),
    "intervention": ("device", "alarm", "message"),
    "maintenance": ("scheduled", "ordinary", "preventive", "lubrication"),
    "main": ("closing", "operator", "panel"),
    "parameter": ("configuration", "parameters", "recipe", "setup"),
    "parameters": ("configuration", "recipe", "setup"),
    "phase": ("sequence", "power", "voltage"),
    "pressure": ("gripper", "closure", "adjustment"),
    "siren": ("flashing", "light", "column", "alarm"),
    "spring": ("compensating", "removal"),
    "springs": ("compensating", "removal"),
    "torque": ("adjustment", "head", "screwing", "cap"),
}
PHRASE_EXPANSIONS = (
    ("main machine controls", ("closing", "machine", "controls", "operator", "panel", "icons")),
    ("machine controls", ("closing", "machine", "controls", "operator", "panel", "icons")),
    ("foil check intervention", ("foil", "presence", "check", "device", "message")),
    ("flashing light siren", ("flashing", "light", "siren", "column", "alarm")),
    ("gripper pressure", ("closure", "gripper", "pressure", "cap", "adjustment")),
    ("configuration parameters", ("configuration", "parameters", "recipe", "setup")),
    ("correct phase sequence", ("verification", "phase", "sequence", "power", "voltage")),
)


def search_manual(
    machine_id: str,
    query: str,
    limit: int = 3,
    *,
    manual_version: str | None = None,
    language: str | None = None,
) -> list[dict]:
    limit = max(1, min(limit, 10))
    filters = _manual_filters(machine_id, manual_version, language)

    # An alarm question is searched twice: once as asked, and once as the
    # manual would put it. The dataset names an alarm ALnnn_MNEMONIC, and the
    # manuals contain neither the code nor usually the mnemonic verbatim - they
    # describe the condition in a fault table. Searching only the literal
    # question leaves that table out of the candidate set entirely, and no
    # amount of reranking can recover a passage that was never retrieved.
    alarm_phrases = _alarm_phrases(query)
    lexical_documents = _lexical_search(filters, query, limit=max(limit, 6))
    for phrase in alarm_phrases:
        lexical_documents.extend(_lexical_search(filters, phrase, limit=max(limit, 6)))
    lexical_documents = _sanitize_documents(lexical_documents)

    ranked_lexical = _rerank_documents(query, _dedupe_documents(lexical_documents))
    if ranked_lexical and _is_high_confidence_lexical_hit(query, ranked_lexical[0]):
        return ranked_lexical[:limit]

    vector = _embed_query(" ".join([query, *alarm_phrases]) if alarm_phrases else query)
    payload = {
        "query": vector,
        "filter": {"must": filters},
        "with_payload": True,
        "limit": max(limit, min(limit * 4, 12)),
    }
    response = _post_json(
        f"{_qdrant_url()}/collections/{_collection()}/points/query",
        payload,
    )
    points = response.get("result", {}).get("points", response.get("result", []))
    vector_documents = [_point_to_document(point) for point in points]
    combined = _sanitize_documents(
        _dedupe_documents([*lexical_documents, *vector_documents])
    )
    return _rerank_documents(query, combined)[:limit]


def probe_qdrant(*, timeout_seconds: float = 0.75) -> None:
    """Is the vector store reachable.

    Deliberately the server and not the manual collection. That collection is
    created by the ingester, which runs after this service starts, so asking for
    it made readiness depend on work that had not happened yet: on a fresh
    volume the 404 was reported as an unavailable dependency for the whole
    twelve minutes ingestion takes. An empty index is a legitimate state to be
    ready in.

    Listing collections rather than /readyz because /readyz answers in plain
    text ("all shards are ready") and this client requires JSON.
    """
    _get_json(f"{_qdrant_url()}/collections", timeout_seconds=timeout_seconds)


def probe_embedding_model(*, timeout_seconds: float = 0.75) -> None:
    """Is the embedding model installed.

    Checked by asking what Ollama has, not by embedding something. A cold model
    takes twenty-odd seconds to load on its first call, which no readiness probe
    should be waiting for - that is a latency question, not a readiness one, and
    the probe used to be given 0.9 seconds to answer it.
    """
    payload = _get_json(f"{_ollama_url()}/api/tags", timeout_seconds=timeout_seconds)
    wanted = _embed_model()
    installed = {
        str(item.get("model") or item.get("name") or "")
        for item in payload.get("models") or []
        if isinstance(item, dict)
    }
    # Ollama reports "nomic-embed-text:latest" for a model pulled as
    # "nomic-embed-text", so an exact match is not enough.
    if not any(name == wanted or name.split(":")[0] == wanted.split(":")[0] for name in installed):
        raise RuntimeError(f"Embedding model {wanted} is not installed.")


def _manual_filters(
    machine_id: str,
    manual_version: str | None,
    language: str | None,
) -> list[dict]:
    filters = [{"key": "machineId", "match": {"value": machine_id}}]
    if manual_version:
        filters.append({"key": "manualVersion", "match": {"value": manual_version}})
    if language:
        filters.append({"key": "language", "match": {"value": language}})
    return filters


def _lexical_search(filters: list[dict], query: str, *, limit: int) -> list[dict]:
    query_text = _normalize(query)
    query_phrase = " ".join(_ordered_terms(query))
    query_terms = _expanded_terms(query)
    if not query_terms:
        return []

    points: list[dict] = []
    offset = None
    while True:
        payload = {
            "filter": {"must": filters},
            "limit": 256,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            payload["offset"] = offset
        response = _post_json(
            f"{_qdrant_url()}/collections/{_collection()}/points/scroll",
            payload,
        )
        result = response.get("result") or {}
        page = result.get("points") or []
        points.extend(page)
        offset = result.get("next_page_offset")
        if offset is None:
            break

    scored: list[tuple[int, dict]] = []
    for point in points:
        document = _point_to_document(point)
        score = _lexical_score(query_text, query_phrase, query_terms, document)
        if score < MIN_LEXICAL_SCORE:
            continue
        confidence = round(min(0.99, 0.72 + score / 200), 3)
        document["confidence"] = confidence
        document["score"] = confidence
        scored.append((score, document))
    scored.sort(key=lambda item: (item[0], item[1].get("page") or 0), reverse=True)
    return [document for _, document in scored[:limit]]


def _embed_query(query: str, *, timeout_seconds: float = 30) -> list[float]:
    payload = {"model": _embed_model(), "input": [query]}
    response = _post_json(
        f"{_ollama_url()}/api/embed",
        payload,
        timeout_seconds=timeout_seconds,
    )
    embeddings = response.get("embeddings")
    if not isinstance(embeddings, list) or not embeddings or not embeddings[0]:
        raise RuntimeError("Embedding model returned no vector.")
    return embeddings[0]


def _post_json(url: str, payload: dict, *, timeout_seconds: float = 30) -> dict:
    api_request = request.Request(
        url,
        data=json.dumps(payload).encode("utf8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(api_request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf8"))


def _get_json(url: str, *, timeout_seconds: float) -> dict:
    api_request = request.Request(url, headers={"Accept": "application/json"})
    with request.urlopen(api_request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Dependency returned an invalid readiness response.")
    return payload


def _point_to_document(point: dict) -> dict:
    payload = point.get("payload") or {}
    raw_score = point.get("score")
    score = float(raw_score) if raw_score is not None else None
    return {
        "machineId": payload.get("machineId"),
        "source": "manual",
        "title": payload.get("section") or payload.get("title") or "Manual citation",
        "page": payload.get("pageStart"),
        "excerpt": _clean_text(payload.get("text", "")),
        "manualVersion": payload.get("manualVersion"),
        "language": payload.get("language"),
        "sourceUri": payload.get("sourceUri"),
        "chunkId": payload.get("chunkId"),
        "section": payload.get("section"),
        "score": score,
        "confidence": max(0.0, min(0.99, score)) if score is not None else None,
        "chunkKind": payload.get("chunkKind"),
        "topics": payload.get("topics") or [],
        "alarmCodes": payload.get("alarmCodes") or [],
        "safetyLevel": payload.get("safetyLevel"),
    }


def _rerank_documents(query: str, documents: list[dict]) -> list[dict]:
    query_terms = _expanded_terms(query)
    query_text = _normalize(query)
    # Read from the raw query: normalising splits the code from its mnemonic.
    alarm_phrases = _alarm_phrases(query)
    if not query_terms:
        return documents
    return sorted(
        documents,
        key=lambda document: (
            _lexical_score(
                query_text,
                " ".join(_ordered_terms(query)),
                query_terms,
                document,
                alarm_phrases,
            ),
            document.get("score") or 0.0,
        ),
        reverse=True,
    )


_BOILERPLATE_LINES = (
    re.compile(
        r"^AROL S\.p\.A\.\s*-\s*teaching copy, Politecnico di Torino, "
        r"System and Device Programming\. Do not redistribute\.?$",
        re.I,
    ),
    re.compile(
        r"^COPYRIGHT BY CLOSYS S\.r\.l\. NO REPRODUCTION OR CHANGE.*ALLOWED$",
        re.I,
    ),
    re.compile(r"^-+\s*Blank page\s*-+$", re.I),
)
_NON_OPERATIONAL_LABELS = (
    "notice educational use only",
    "report of the training",
    "training report",
)
_TRAINING_FORM_MARKERS = (
    "trainer/s signature",
    "trainer's signature",
    "name charge signature",
)
_DOT_LEADER = re.compile(r"(?:\.\s*){12,}")


def _clean_manual_excerpt(value: str) -> str:
    lines: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if any(pattern.fullmatch(stripped) for pattern in _BOILERPLATE_LINES):
            continue
        lines.append(stripped)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _sanitize_documents(documents: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for document in documents:
        label = " ".join(
            str(value)
            for value in (document.get("title"), document.get("section"))
            if value
        ).lower()
        if any(marker in label for marker in _NON_OPERATIONAL_LABELS):
            continue

        excerpt = _clean_manual_excerpt(str(document.get("excerpt") or ""))
        normalized = re.sub(r"\s+", " ", excerpt).strip().lower()
        if not normalized:
            continue
        if "no operational reliance" in normalized or any(
            marker in normalized for marker in _TRAINING_FORM_MARKERS
        ):
            continue
        if _DOT_LEADER.search(excerpt):
            continue
        words = re.findall(r"[a-z0-9]+", normalized)
        if len(words) < 3 or len(normalized) < 16:
            continue

        sanitized.append({**document, "excerpt": excerpt})
    return sanitized


#: ``ALnnn_MNEMONIC``. The dataset names alarms this way; the manuals do not
#: contain the code, and usually not the mnemonic verbatim either.
_ALARM_CODE = re.compile(r"\bAL\d{2,}_([A-Z][A-Z0-9_]*)\b")


def _alarm_phrases(query: str) -> tuple[str, ...]:
    """The words a manual would use for an alarm named in the dataset's terms."""
    phrases = tuple(
        " ".join(part.lower() for part in match.group(1).split("_") if part)
        for match in _ALARM_CODE.finditer(query)
    )
    # Platform conditions and manual terminology differ. Keep the component
    # explicit; never expand a pressure alarm to arbitrary pressure settings.
    aliases = {
        'low air pressure': ('air supply pressure', 'air pressure', 'working pressure'),
        'minimum caps level': ('caps sorter is sufficiently full', 'caps level', 'caps feeding'),
        'caps sorter upper door open': ('caps sorter', 'safety doors', 'safety guards'),
        'entrance tunnel open': ('entrance tunnel', 'safety guards', 'safety devices'),
        'bottle too high': ('container height', 'bottle height'),
        'height reg motor overload': ('height adjustment motor', 'motor overload'),
        'line emergency pressed': ('emergency stop', 'emergency button'),
    }
    return tuple(phrase for original in phrases for phrase in (original, *aliases.get(original, ())))


def _alarm_phrase_score(alarm_phrases: tuple[str, ...], document: dict) -> int:
    """Rank an alarm question by the condition, not by the code.

    Vector similarity alone answers "what does AL017_LOW_AIR_PRESSURE mean" with
    whatever prose sits nearest in embedding space, which on a real manual was
    roller height adjustment: fluent, plausible and about something else. The
    mnemonic's words are the real query, and the manuals put the answer in a
    fault table.
    """
    if not alarm_phrases:
        return 0

    source = _normalize(
        f"{document.get('title') or ''} {document.get('section') or ''} "
        f"{document.get('excerpt') or ''}"
    )
    words = {word for phrase in alarm_phrases for word in phrase.split()}
    matched = sum(1 for word in words if word in source)

    score = 12 * matched
    if any(phrase in source for phrase in alarm_phrases):
        score += 60
    if document.get("chunkKind") in {"troubleshooting", "table"}:
        score += 25
    if not matched:
        score -= 50
    return score


def _lexical_score(
    query_text: str,
    query_phrase: str,
    query_terms: set[str],
    document: dict,
    alarm_phrases: tuple[str, ...] = (),
) -> int:
    excerpt = _normalize(str(document.get("excerpt") or ""))
    title = _normalize(
        " ".join(
            str(value)
            for value in (document.get("title"), document.get("section"))
            if value
        )
    )
    excerpt_terms = set(excerpt.split())
    title_terms = set(title.split())
    score = 0
    if query_text and query_text in excerpt:
        score += 80
    if query_phrase and query_phrase in excerpt:
        score += 80
    if title and title in query_text:
        score += 50
    if query_phrase and title and title in query_phrase:
        score += 50
    if query_text and query_text in f"{title} {excerpt}":
        score += 35
    source_text = f"{title} {excerpt}"
    score += _error_code_score(query_text, source_text)
    score += _domain_phrase_score(query_text, source_text)
    score += _metadata_score(query_text, query_terms, document)
    score += _alarm_phrase_score(alarm_phrases, document)
    score += len(query_terms.intersection(excerpt_terms))
    score += 6 * len(query_terms.intersection(title_terms))
    if len(query_terms) >= 3 and query_terms.issubset(excerpt_terms):
        score += 20
    if "...." in str(document.get("excerpt") or ""):
        score -= 40
    if "manual is property" in excerpt:
        score -= 20
    return score


def _domain_phrase_score(query_text: str, source_text: str) -> int:
    return 60 * sum(
        1
        for phrase, _additions in PHRASE_EXPANSIONS
        if phrase in query_text and phrase in source_text
    )


def _metadata_score(query_text: str, query_terms: set[str], document: dict) -> int:
    score = 0
    topics = set(document.get("topics") or [])
    overlap = query_terms.intersection(topics)
    score += 10 * len(overlap)
    if topics and overlap == topics and len(topics) >= 2:
        score += 12
    chunk_kind = document.get("chunkKind")
    if chunk_kind in {"procedure", "troubleshooting"} and query_terms.intersection(
        ALARM_QUERY_TERMS.union(PROCEDURAL_QUERY_TERMS)
    ):
        score += 12
    if chunk_kind == "safety" and query_terms.intersection(SAFETY_QUERY_TERMS):
        score += 14
    if document.get("safetyLevel") in {"technician", "safety-critical"} and (
        query_terms.intersection(SAFETY_QUERY_TERMS)
    ):
        score += 12
    code_texts = {_normalize(code) for code in document.get("alarmCodes") or []}
    score += 40 * len({code for code in code_texts if code and code in query_text})
    code_terms = {term for code in code_texts for term in code.split() if term}
    if code_terms.intersection(query_terms) and query_terms.intersection(ALARM_QUERY_TERMS):
        score += 18
    return score


def _error_code_score(query_text: str, source_text: str) -> int:
    query_codes = set(re.findall(r"\berror\s+\d+\b", query_text))
    if not query_codes:
        return 0
    source_codes = set(re.findall(r"\berror\s+\d+\b", source_text))
    matched = len(query_codes.intersection(source_codes))
    other = len(source_codes.difference(query_codes))
    return 30 * matched - (18 if matched == 0 and other else 0)


def _is_high_confidence_lexical_hit(query: str, document: dict) -> bool:
    if (document.get("score") or document.get("confidence") or 0.0) >= 0.9:
        return True
    query_text = _normalize(query)
    source_text = _normalize(
        f"{document.get('title') or ''} {document.get('section') or ''} "
        f"{document.get('excerpt') or ''}"
    )
    query_codes = set(re.findall(r"\berror\s+\d+\b", query_text))
    if query_codes and query_codes.issubset(
        set(re.findall(r"\berror\s+\d+\b", source_text))
    ):
        return True
    evidence_codes = {_normalize(code) for code in document.get("alarmCodes") or []}
    if evidence_codes and any(code in query_text for code in evidence_codes):
        return True
    return bool(query_text and query_text in source_text)


def _dedupe_documents(documents: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for document in documents:
        key = document.get("chunkId") or (
            f"{document.get('sourceUri')}:{document.get('page')}:"
            f"{document.get('section')}:{str(document.get('excerpt'))[:40]}"
        )
        existing = deduped.get(key)
        if existing is None or (document.get("score") or 0.0) > (
            existing.get("score") or 0.0
        ):
            deduped[key] = document
    return list(deduped.values())


def _clean_text(value: str) -> str:
    lines = [
        line.strip()
        for line in str(value).splitlines()
        if not line.strip().lower().startswith("this manual is property of arol")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _normalize(value: str) -> str:
    normalized = _clean_text(value).lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _ordered_terms(value: str) -> list[str]:
    return list(
        dict.fromkeys(
            token
            for token in _normalize(value).split()
            if len(token) > 1 and token not in STOP_WORDS
        )
    )


def _expanded_terms(value: str) -> set[str]:
    terms = set(_ordered_terms(value))
    expanded = set(terms)
    for term in terms:
        expanded.update(TERM_EXPANSIONS.get(term, ()))
    normalized = _normalize(value)
    for phrase, additions in PHRASE_EXPANSIONS:
        if phrase in normalized:
            expanded.update(additions)
    return expanded


def _qdrant_url() -> str:
    return os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def _embed_model() -> str:
    return os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")


def _collection() -> str:
    return os.environ.get("DOC_COLLECTION", "manual_chunks")
