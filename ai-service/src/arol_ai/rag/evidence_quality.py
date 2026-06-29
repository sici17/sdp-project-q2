import re
from dataclasses import replace

from arol_ai.graph.state import Evidence

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


def clean_manual_excerpt(value: str) -> str:
    """Remove publisher/course boilerplate while preserving technical text."""

    lines: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if any(pattern.fullmatch(stripped) for pattern in _BOILERPLATE_LINES):
            continue
        lines.append(stripped)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def is_substantive_manual_text(value: str, *, label: str = "") -> bool:
    """True when this manual text can support an operational answer.

    Retrieval and citation must agree on this. When only the citation step
    knew it, a page of contents or of publisher boilerplate still took a slot
    in the top results and was discarded afterwards, so an answer that had a
    usable passage further down the ranking ended up citing nothing at all.
    """

    if any(marker in label.lower() for marker in _NON_OPERATIONAL_LABELS):
        return False

    excerpt = clean_manual_excerpt(value)
    normalized = re.sub(r"\s+", " ", excerpt).strip().lower()
    if not normalized:
        return False
    if "no operational reliance" in normalized or any(
        marker in normalized for marker in _TRAINING_FORM_MARKERS
    ):
        return False

    # A contents page names every section in the manual, so it matches almost
    # any query's vocabulary while supporting no answer.
    if _DOT_LEADER.search(excerpt):
        return False

    words = re.findall(r"[a-z0-9]+", normalized)
    return len(words) >= 3 and len(normalized) >= 16


def is_substantive_manual_evidence(evidence: Evidence) -> bool:
    """True only for a manual passage that can support an operational answer."""

    if evidence.source != "manual":
        return True

    label = " ".join(part for part in (evidence.title, evidence.section or "") if part)
    return is_substantive_manual_text(evidence.excerpt, label=label)


#: Chunks from one manual page that may appear in a single answer.
#:
#: A page is split into overlapping chunks, so a page whose vocabulary matches
#: the question tends to match it several times over. One page then took three
#: of seven evidence slots, crowding out the other pages that were also
#: relevant, and the operator saw the same passage cited three times.
MAX_CHUNKS_PER_PAGE = 2


def sanitize_manual_evidence(items: list[Evidence]) -> list[Evidence]:
    """Drop non-operational chunks and remove boilerplate from retained ones."""

    sanitized: list[Evidence] = []
    per_page: dict[int, int] = {}
    for item in items:
        if item.source != "manual":
            sanitized.append(item)
            continue

        candidate = replace(item, excerpt=clean_manual_excerpt(item.excerpt))
        if not is_substantive_manual_evidence(candidate):
            continue

        if candidate.page is not None:
            seen = per_page.get(candidate.page, 0)
            if seen >= MAX_CHUNKS_PER_PAGE:
                continue
            per_page[candidate.page] = seen + 1

        sanitized.append(candidate)

    return sanitized
