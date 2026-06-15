import re

BOILERPLATE_PATTERNS = (
    re.compile(r"^this manual is property of arol.*$", re.I),
    re.compile(r"^this manual is property of arol s\.p\.a\..*$", re.I),
)


def clean_manual_text(value: str) -> str:
    lines = []
    for line in value.splitlines():
        normalized = line.strip()
        if not normalized:
            lines.append("")
            continue

        if _is_boilerplate(normalized):
            continue

        lines.append(normalized)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def normalize_for_search(value: str) -> str:
    normalized = clean_manual_text(value).lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def search_tokens(value: str) -> set[str]:
    return set(ordered_search_terms(value))


def expanded_search_tokens(value: str) -> set[str]:
    terms = set(ordered_search_terms(value))
    expanded = set(terms)

    for term in terms:
        expanded.update(TERM_EXPANSIONS.get(term, ()))

    normalized = normalize_for_search(value)
    for phrase, additions in PHRASE_EXPANSIONS:
        if phrase in normalized:
            expanded.update(additions)

    return expanded


def ordered_search_terms(value: str) -> list[str]:
    terms = [
        token
        for token in normalize_for_search(value).split()
        if len(token) > 1 and token not in STOP_WORDS
    ]
    return list(dict.fromkeys(terms))


def _is_boilerplate(line: str) -> bool:
    if line.isdigit() or re.fullmatch(r"[ivxlcdm]+", line, re.I):
        return True

    return any(pattern.match(line) for pattern in BOILERPLATE_PATTERNS)


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


# An alarm condition can be named two ways in this corpus, and retrieval has to
# treat both as the same discriminating token:
#
#   AL017_LOW_AIR_PRESSURE   the fleet dataset's code, normalizing to "al017 ..."
#   ERROR 20                 the form used inside the older machine manuals
#
# Matching only the second is what made a question about AL017 score no better
# against the right manual row than against any other row.
_CODE_PATTERNS = (
    # "error 20", "alarm 15", optionally "error code 20"
    re.compile(r"\b(?:error|alarm)\s+(?:code\s+)?(\d{1,4})\b"),
    # "al017", "e20" - a short letter prefix bound to a number
    re.compile(r"\b([a-z]{1,4}\d{1,4})\b"),
)


def extract_code_tokens(text: str) -> set[str]:
    """Normalized alarm/error identifiers mentioned in ``text``.

    Expects text that has already been through :func:`normalize_for_search`.
    Both spellings collapse to a single comparable token, so an operator can
    type the dataset code or the manual's wording and reach the same rows.
    """
    tokens: set[str] = set()
    for match in _CODE_PATTERNS[0].finditer(text):
        tokens.add(match.group(1))
    for match in _CODE_PATTERNS[1].finditer(text):
        token = match.group(1)
        # Keep the digits alone as well, so "al017" and "error 17" meet.
        tokens.add(token)
        digits = re.sub(r"^[a-z]+", "", token)
        if digits:
            tokens.add(digits.lstrip("0") or digits)
    return {token for token in tokens if token}


# The dataset names an alarm as ALnnn_MNEMONIC, where the mnemonic is the short
# textual description of the physical condition. The manuals never contain the
# code itself: to resolve an alarm you search them for that description. So the
# mnemonic, not the code, is the search intent.
_SYMBOLIC_ALARM = re.compile(r"\b[A-Z]{2,}\d{1,4}_([A-Z0-9_]+)\b")


def carries_alarm_description(text: str, phrases: tuple[str, ...]) -> bool:
    """True when ``text`` is about the condition an alarm code names.

    The fleet dataset's codes are never printed in the manuals, so an exact
    code match cannot be the only way to accept a passage. The mnemonic is the
    bridge: ``AL017_LOW_AIR_PRESSURE`` is explained by the page that discusses
    low air pressure. Requiring the whole phrase, or every one of its
    significant words, is what separates that page from one that merely shares
    a common word with the code.
    """
    if not phrases:
        return False

    haystack = normalize_for_search(text)
    aliases = {
        "low air pressure": ("air supply pressure", "air pressure", "working pressure"),
        "minimum caps level": ("caps sorter is sufficiently full", "caps level", "caps feeding"),
        "caps sorter upper door open": ("caps sorter", "safety doors", "safety guards"),
        "entrance tunnel open": ("entrance tunnel", "safety guards", "safety devices"),
        "bottle too high": ("container height", "bottle height"),
        "height reg motor overload": ("height adjustment motor", "motor overload"),
        "line emergency pressed": ("emergency stop", "emergency button"),
    }
    phrases = tuple(
        phrase for original in phrases for phrase in (original, *aliases.get(original, ()))
    )
    for phrase in phrases:
        if phrase in haystack:
            return True
        terms = [term for term in phrase.split() if len(term) > 2]
        if terms and all(term in haystack for term in terms):
            return True
    return False


def alarm_code_phrases(value: str) -> tuple[str, ...]:
    """Human phrases carried by any ALnnn_MNEMONIC codes in ``value``.

    ``AL017_LOW_AIR_PRESSURE`` yields ``("low air pressure",)``, which is what
    the machine's manual actually talks about.
    """
    phrases = []
    for match in _SYMBOLIC_ALARM.finditer(value):
        phrase = normalize_for_search(match.group(1).replace("_", " "))
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return tuple(phrases)


def focus_alarm_passage(text: str, query: str) -> str:
    """Return only the table entry named by a symbolic alarm, when possible.

    PDF extraction often flattens several adjacent alarm rows into one chunk.
    Sending the whole chunk to a summarizer lets a neighboring remedy leak into
    an otherwise correct answer. The AL mnemonic provides a precise condition
    phrase; use the nearest numbered-row boundaries around that phrase.
    """
    target = None
    for phrase in alarm_code_phrases(query):
        words = [re.escape(word) for word in phrase.split() if word]
        if not words:
            continue
        target = re.search(r"\b" + r"[\s_/-]+".join(words) + r"\b", text, re.I)
        if target:
            break

    if target is None:
        return text

    boundaries = sorted(
        {
            match.start()
            for pattern in (
                r"(?:^|\|)\s*\d{1,3}\s*\|\s*(?=[A-Z])",
                r"(?:^|\s)\d{1,3}\s+(?=[A-Z][A-Z0-9 ()/_-]{3,})",
            )
            for match in re.finditer(pattern, text)
        }
    )
    starts = [position for position in boundaries if position <= target.start()]
    ends = [position for position in boundaries if position > target.start()]
    start = starts[-1] if starts else target.start()
    end = ends[0] if ends else len(text)
    focused = text[start:end].strip(" |\n\t")
    return focused or text


#: PDF furniture that reaches an operator as if it were a procedure: a leading
#: page number and subsection index ("88 11.1.3"), a bare page number on its own
#: line, the copyright footer, and the pipe-delimited rows table extraction
#: produces.
_LEADING_INDEX = re.compile(r"^\d{1,3}\s+(?=\d+\.)")
_SUBSECTION = re.compile(r"^\d+(?:\.\d+)+\s*")
_FIGURE = re.compile(r"\bFig\.\s*\d+", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.:;])\s+")


def readable_passage(excerpt: str, *, limit: int = 600) -> str:
    """A retrieved passage as prose, rather than as extracted layout.

    Retrieval hands back what the PDF contains: the page number, the subsection
    index, figure callouts, and table rows rendered with pipes. Passed straight
    to an operator those read as part of the procedure, and a hard character cut
    finished the job by ending mid-word. This keeps whole sentences and drops
    the furniture around them.
    """
    kept: list[str] = []
    for line in (excerpt or "").splitlines():
        line = line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("section:") or "this manual is property of arol" in lowered:
            continue
        if line.isdigit():
            continue
        # A table row is data, not a sentence; it is unreadable run together.
        if line.count("|") >= 2:
            continue
        line = _LEADING_INDEX.sub("", line)
        line = _SUBSECTION.sub("", line)
        line = _FIGURE.sub("", line)
        line = line.strip()
        if line:
            kept.append(line)

    text = " ".join(kept).strip()
    if len(text) <= limit:
        return text

    # Cut on a sentence boundary so the passage ends where a sentence does.
    out = ""
    for part in _SENTENCE_END.split(text):
        if len(out) + len(part) + 1 > limit:
            break
        out = f"{out} {part}".strip()
    return out or text[:limit].rsplit(" ", 1)[0]
