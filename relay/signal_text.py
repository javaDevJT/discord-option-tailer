"""Small, dependency-free text classifiers shared by relay interpreters."""

from __future__ import annotations

import re
import unicodedata


_NUMBER = r"[+-]?\d+(?:\.\d+)?"
_PERCENT = rf"{_NUMBER}\s*%+"
_GAIN_TAILS = (
    r"here(?:\s+fire+)?",
    r"ez\s+af",
    r"slow\s+ass\s+contract",
    r"but\s+these\s+wicks\s+are\s+insane",
    r"and\s+more\s+too\s+easy",
    r"fucking",
    r"lfg+",
    r"gg\s+what\s+a\s+good\s+ass\s+friday",
)
_GAIN_TAIL = "(?:" + "|".join(_GAIN_TAILS) + ")"
_GAIN_STATUS = re.compile(
    rf"(?:{_PERCENT}(?:\s*{_GAIN_TAIL})?|{_NUMBER}\s+fucking\s+%+)\s*!*",
    re.IGNORECASE,
)
_LOUD_GAIN_STATUS = re.compile(rf"fu{{2,}}\s+{_PERCENT}\s*!*", re.IGNORECASE)
_ROLE_MENTION = re.compile(r"<@&\d+>")


def _without_emoji(text: str) -> str:
    return "".join(
        " " if unicodedata.category(char) in {"So", "Sk", "Cf"} or char == "\ufe0f" else char
        for char in text
    )


def is_gain_status(text: str) -> bool:
    """Recognize only the source's bounded percentage-status comment shapes.

    The classifier does not infer status from an arbitrary percentage. The
    whole comment must match a captured gain/status shape, so conditions,
    stops, position sizing, mixed exits, and unfamiliar prose remain eligible
    for the normal interpretation path.
    """
    if not isinstance(text, str) or len(text) > 6000 or "\n" in text or "\r" in text:
        return False
    if "http://" in text.casefold() or "https://" in text.casefold():
        return False
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _ROLE_MENTION.sub(" ", normalized)
    normalized = _without_emoji(normalized).casefold().strip()
    return bool(_GAIN_STATUS.fullmatch(normalized) or _LOUD_GAIN_STATUS.fullmatch(normalized))


__all__ = ["is_gain_status"]
