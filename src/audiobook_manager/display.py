from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from collections.abc import Callable

SPACE = re.compile(r"\s+")


def display_key(value: object) -> str:
    """Return a conservative identity used only to reconcile display casing."""
    text = unicodedata.normalize("NFKC", str(value))
    return SPACE.sub(" ", text).strip().casefold()


def person_key(value: object) -> str:
    """Normalize harmless punctuation and spacing differences in person names."""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def _display_quality(value: str) -> tuple[int, int, int, int]:
    letters = [character for character in value if character.isalpha()]
    has_upper = any(character.isupper() for character in letters)
    has_lower = any(character.islower() for character in letters)
    words = re.findall(r"[^\W\d_]+", value, flags=re.UNICODE)
    initial_capitals = sum(bool(word) and word[0].isupper() for word in words)
    return (2 if has_upper and has_lower else 1 if has_upper else 0,
            initial_capitals, value.count(". "), sum(character.isupper() for character in letters))


def prefer_display(value: str, alternatives: Iterable[object],
                   identity: Callable[[object], str] = display_key) -> str:
    """Choose the best-presented spelling without changing textual identity."""
    key = identity(value)
    matching = [str(candidate).strip() for candidate in alternatives
                if str(candidate).strip() and identity(candidate) == key]
    return max([value.strip(), *matching], key=_display_quality)
