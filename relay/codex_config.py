"""Small validation helpers for Codex model and configuration identifiers."""

import unicodedata


def is_valid_codex_identifier(value: object) -> bool:
    """Accept a bounded identifier without whitespace or control characters."""
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value[0].isalnum():
        return False
    return all(not char.isspace() and not unicodedata.category(char).startswith("C") for char in value)
