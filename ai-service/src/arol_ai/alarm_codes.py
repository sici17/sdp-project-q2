import re

ALARM_CODE_PATTERN = re.compile(
    r"(?<![A-Z0-9])AL[\s_-]?(\d{3})(?=$|[^0-9])",
    re.IGNORECASE,
)


def extract_alarm_code(text: str) -> str | None:
    """Return the canonical short code from operator text, for example ``AL031``."""
    match = ALARM_CODE_PATTERN.search(text)
    return f"AL{match.group(1)}" if match else None


def contains_alarm_code(text: str | None, requested_code: str) -> bool:
    """Match a short code against text or a full mnemonic without substring collisions."""
    if not text:
        return False
    requested = extract_alarm_code(requested_code)
    if requested is None:
        return False
    return any(f"AL{match.group(1)}" == requested for match in ALARM_CODE_PATTERN.finditer(text))


def alarm_condition_label(alarm_code: str) -> str:
    """Turn ``AL031_HEADS_MOTOR_OVERLOAD`` into ``Heads motor overload``."""
    _, separator, mnemonic = alarm_code.partition("_")
    if not separator or not mnemonic:
        return alarm_code
    return mnemonic.replace("_", " ").lower().capitalize()
