import re

from arol_ai.rag.text import normalize_for_search, ordered_search_terms

CHUNK_KIND_VALUES = ("procedure", "troubleshooting", "safety", "reference", "table")
SAFETY_TERMS = {
    "danger",
    "warning",
    "guard",
    "interlock",
    "emergency",
    "safety",
    "stop",
    "voltage",
}
PROCEDURE_TERMS = {"procedure", "step", "check", "inspect", "verify", "adjust", "confirm"}
TROUBLESHOOTING_TERMS = {
    "alarm",
    "error",
    "fault",
    "troubleshoot",
    "failure",
    # How the AROL manuals actually head their fault tables.
    "message",
    "problem",
    "cause",
    "solution",
    "remedy",
    "malfunction",
}
TOPIC_TERMS = {
    "alarm",
    "cap",
    "configuration",
    "controls",
    "emergency",
    "foil",
    "gripper",
    "inspection",
    "interlock",
    "lubrication",
    "maintenance",
    "manual",
    "phase",
    "pressure",
    "recipe",
    "safety",
    "siren",
    "torque",
}


def chunk_kind(*, section: str, text: str) -> str:
    normalized = normalize_for_search(f"{section} {text}")
    terms = set(normalized.split())

    if _looks_like_table(text):
        return "table"

    if terms.intersection(SAFETY_TERMS):
        return "safety"

    if terms.intersection(TROUBLESHOOTING_TERMS):
        return "troubleshooting"

    if terms.intersection(PROCEDURE_TERMS):
        return "procedure"

    return "reference"


def extract_topics(*, section: str, text: str) -> tuple[str, ...]:
    terms = set(ordered_search_terms(f"{section} {text}"))
    return tuple(sorted(terms.intersection(TOPIC_TERMS)))


def extract_alarm_codes(value: str) -> tuple[str, ...]:
    normalized_codes = []
    prefixed_pattern = r"\b(error|alarm)\s+(?:code\s+)?([a-z0-9_-]+)\b"
    # ALnnn_MNEMONIC as used by the fleet dataset, and plain WORD_WORD codes.
    symbolic_pattern = r"\b([A-Z]{2,}\d*_[A-Z0-9_]+)\b"

    for match in re.finditer(prefixed_pattern, value, re.I):
        prefix = match.group(1).strip().upper()
        raw_code = match.group(2).strip()
        if not _looks_like_alarm_code(raw_code):
            continue

        code = raw_code.upper().replace("-", "_")
        if code.isdigit():
            code = f"{prefix}_{code}"
        if code and code not in normalized_codes:
            normalized_codes.append(code)

    for match in re.finditer(symbolic_pattern, value):
        code = match.group(1).strip().upper().replace("-", "_")
        if code and code not in normalized_codes:
            normalized_codes.append(code)

    return tuple(normalized_codes)


def _looks_like_alarm_code(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False

    if any(character.isdigit() for character in stripped):
        return True

    if "_" in stripped or "-" in stripped:
        return True

    return False


def safety_level(*, section: str, text: str) -> str:
    normalized = normalize_for_search(f"{section} {text}")
    terms = set(normalized.split())

    if {"danger", "emergency", "interlock", "voltage"}.intersection(terms):
        return "safety-critical"

    if {"warning", "guard", "safety"}.intersection(terms):
        return "technician"

    return "operator"


def _looks_like_table(value: str) -> bool:
    lines = [line for line in value.splitlines() if line.strip()]
    if len(lines) < 3:
        return False

    aligned_rows = sum(1 for line in lines if len(re.split(r"\s{2,}|\t", line.strip())) >= 3)
    return aligned_rows >= 3
