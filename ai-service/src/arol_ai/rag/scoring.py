"""Vocabulary shared by every lexical scorer in the service.

Three scorers rank manual passages: the PDF-reading one in ``lexical.py`` that
the offline benchmark uses, the Qdrant scroll in ``qdrant_store.py``, and the
reranker in ``retriever.py`` that runs over whatever the other two returned.
They score different shapes - a chunk, a payload dict, an ``Evidence`` - so the
scoring functions stay separate. The vocabulary they score against does not:
when these lists lived in three files they had already begun to drift, and a
term added to one silently changed how one leg ranked and not the others.

Doc MCP keeps its own copy in ``mcp-servers/doc-mcp/retrieval/search.py``. It
is a separately deployed service with no import path into this package, so that
duplication is deliberate; it is noted there.
"""

from arol_ai.rag.text import extract_code_tokens

#: Below this a lexical match is noise rather than a hit.
MIN_LEXICAL_SCORE = 8

#: Words that mark a question as being about an alarm or fault condition.
ALARM_QUERY_TERMS = frozenset(
    {
        "alarm",
        "alarms",
        "code",
        "error",
        "fault",
        "message",
        "troubleshooting",
    }
)

#: Words that mark a question as asking how to carry something out.
PROCEDURAL_QUERY_TERMS = frozenset(
    {
        "adjust",
        "check",
        "confirm",
        "inspect",
        "procedure",
        "verify",
        "what",
    }
)

#: Words that mark a question as touching a guarded or energised system.
SAFETY_QUERY_TERMS = frozenset(
    {
        "door",
        "emergency",
        "guard",
        "interlock",
        "safety",
        "stop",
        "voltage",
    }
)


#: An alarm code the question named and the passage contains.
CODE_MATCH = 30
#: The passage names other codes and none of them are the one asked about.
CODE_MISMATCH_PENALTY = 18


def error_code_score(*, query_text: str, source_text: str) -> int:
    """How well a passage's alarm codes answer the codes in the question.

    Unlike the scorers this module deliberately leaves separate, this one reads
    two strings rather than a chunk, a payload or an Evidence - so the reason
    they stay apart does not apply to it. It had been copied into all three of
    them, byte for byte.
    """
    query_codes = extract_code_tokens(query_text)
    if not query_codes:
        return 0

    source_codes = extract_code_tokens(source_text)
    matched = len(query_codes.intersection(source_codes))
    other = len(source_codes.difference(query_codes))
    score = CODE_MATCH * matched
    if matched == 0 and other:
        score -= CODE_MISMATCH_PENALTY
    return score
