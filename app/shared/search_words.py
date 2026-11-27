"""Free-text search for materials: every word typed has to be found, in any order.

A search box used to look for the whole text as one run of characters, so
``"orifice pressure 1 plug 2mm"`` found nothing against a description written
``"ORIFICE, PRESSURE 1 PLUG, 2MM 1/8"``: the commas break the run. Now the text is
split into words and a material matches when **each** word is part of its material
number or description (case-insensitive). Punctuation around a word is ignored, so
``"plug,"`` and ``"plug"`` are the same word.

Pure: no database, no settings. Both the forward recommendation list (SQL) and the
Min-Max to OAR list (in memory) use it, so the two searches behave the same.
"""

from __future__ import annotations

_EDGE_PUNCTUATION = ".,;:!?\"'`()[]{}<>|\\"

MAX_WORDS = 8
"""More words than this are ignored: a longer search is a pasted sentence, and every
word is one more condition in the query."""


def search_words(text: str | None) -> list[str]:
    """The distinct, lower-cased words of a search, in the order typed.

    Empty when there is nothing to search for (blank text, or only punctuation).
    """
    if text is None:
        return []
    words: list[str] = []
    for raw in text.lower().split():
        word = raw.strip(_EDGE_PUNCTUATION)
        if word and word not in words:
            words.append(word)
        if len(words) == MAX_WORDS:
            break
    return words


def matches_all_words(haystack: str, words: list[str]) -> bool:
    """True when every word is part of ``haystack`` (case-insensitive). No words matches everything."""
    lowered = haystack.lower()
    return all(word in lowered for word in words)
