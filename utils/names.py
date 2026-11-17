"""Capitalization helpers for person names entered during registration."""

import re

# Canonical forms for generational suffixes. Keys are matched case-insensitively.
NAME_SUFFIXES = {
    "jr": "Jr.",
    "jr.": "Jr.",
    "sr": "Sr.",
    "sr.": "Sr.",
    "ii": "II",
    "iii": "III",
    "iv": "IV",
    "v": "V",
    "vi": "VI",
}


def format_person_name(value):
    """Capitalize each name token while preserving punctuation and existing
    intentional mixed casing (e.g. "McDonald", "DelaCruz").

    Whitespace, digits, and separators are left untouched.
    """
    if not isinstance(value, str) or not value:
        return value or ""
    return re.sub(r"\S+", lambda match: _format_token(match.group(0)), value)


def _format_token(token):
    suffix = NAME_SUFFIXES.get(token.casefold())
    if suffix:
        return suffix
    if _has_intentional_casing(token):
        return token
    return _title_case_segments(token)


def _has_intentional_casing(token):
    # CamelCase such as "McDonald": starts uppercase, has another uppercase
    # later, and also contains lowercase letters.
    if len(token) < 2 or not token[0].isupper():
        return False
    rest = token[1:]
    return any(ch.islower() for ch in token) and any(ch.isupper() for ch in rest)


def _title_case_segments(token):
    formatted = []
    at_word_start = True
    for char in token:
        if char in "-'’":
            formatted.append(char)
            at_word_start = True
        elif char.isalpha():
            formatted.append(char.upper() if at_word_start else char.lower())
            at_word_start = False
        else:
            formatted.append(char)
            at_word_start = False
    return "".join(formatted)
