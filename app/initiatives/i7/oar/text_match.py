"""Description normalisation and fuzzy matching for the text dimension.

The text dimension is two signals over the same normalised descriptions::

    S_text = w_e x cosine(BGE embeddings)  +  w_f x fuzzy                         (0.6 / 0.4 by default)
    fuzzy  = (token_set_ratio + token_sort_ratio) / 2 / 100

**The embedding** (BAAI/bge-base-en-v1.5, normalised vectors) says whether two descriptions *mean* alike even
when worded differently. **The fuzzy ratio** says whether they share the same words, which a clipped 40-character
SAP description often leaves as the only reliable evidence -- a part number, a size, a make. Both rapidfuzz
ratios are word-order independent; they differ in what they forgive:

* ``token_set_ratio`` ignores words only one side has: it scores 100 whenever one description's words are all in
  the other's (``FILTER`` against ``FILTER PRESS ANDRITZ SPRAY NOZZLE``).
* ``token_sort_ratio`` sorts the words and compares the whole strings, so every extra word counts against the
  match: the same pair scores about 31.

The average, so a short description that is a word-subset of a longer one is no longer a perfect match (that
pair is about 65), while a reordering of the same words still is (both ratios are 100).

NORMALISATION, applied before either signal (and only to the text dimension -- the identity stage reads the
original description): upper-case, commas and runs of spaces collapsed to single spaces, then the abbreviations
SAP descriptions are full of expanded whole-word (``BUSH`` -> ``BUSHING``, ``PL`` -> ``PLATE``, ``ASSY`` ->
``ASSEMBLY``, ``BRG`` -> ``BEARING``). Whole words only: ``BUSHING`` is left alone, ``PLATE`` is left alone.

WHAT THE FUZZY HALF STILL DOES NOT DO, and why the identity stage still exists. Averaging in ``token_sort_ratio``
stops a subset scoring 100 but not scoring well (about 65 above), and ratios compare characters inside the words,
so descriptions with nothing in common still score a few points. A high text score is a generic word standing
in for identity often enough that it is NOT evidence of the same part, and the identity floors
(``oar.identity``) are measured on this scale, not MiniLM's: they must be set for it
(``SimilarityPolicy.text_floors_calibrated``).

``rapidfuzz`` is required whenever the text dimension is on; without it the dimension reports unavailable rather
than quietly falling back to the embedding alone.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Mapping, Sequence

import numpy as np

NORMALISATION_VERSION = "match-norm-1"
"""Part of the embedding provider's model version, so a change to the rules below invalidates every stored
embedding by construction (``text_similarity.cache_key``)."""

ABBREVIATIONS: Mapping[str, str] = {
    "BUSH": "BUSHING",
    "PL": "PLATE",
    "ASSY": "ASSEMBLY",
    "BRG": "BEARING",
}

_SEPARATORS = re.compile(r"[,\s]+")


def normalise_for_match(
    description: str | None, abbreviations: Mapping[str, str] = ABBREVIATIONS
) -> str | None:
    """Upper-case, commas and extra spaces removed, abbreviations expanded as whole words. ``None`` if empty."""
    if description is None:
        return None
    words = _SEPARATORS.sub(" ", description.upper()).strip().split(" ")
    expanded = " ".join(abbreviations.get(word, word) for word in words if word)
    return expanded or None


def available() -> bool:
    """Whether rapidfuzz can be imported."""
    try:
        import rapidfuzz  # noqa: F401
    except ImportError:
        return False
    return True


def _fuzz():
    try:
        from rapidfuzz import fuzz
    except ImportError as error:  # pragma: no cover - exercised only where the library is absent
        raise RuntimeError(
            "rapidfuzz is required for the text dimension (pip install rapidfuzz); "
            "it is not silently skipped, because the score would then mean something else"
        ) from error
    return fuzz


def token_set_ratio(left: str | None, right: str | None) -> float:
    """rapidfuzz ``token_set_ratio`` of two already-normalised strings, 0..100. 0 when either is missing."""
    if not left or not right:
        return 0.0
    return float(_fuzz().token_set_ratio(left, right))


def token_sort_ratio(left: str | None, right: str | None) -> float:
    """rapidfuzz ``token_sort_ratio`` of two already-normalised strings, 0..100. 0 when either is missing."""
    if not left or not right:
        return 0.0
    return float(_fuzz().token_sort_ratio(left, right))


def fuzzy_ratio(left: str | None, right: str | None) -> tuple[float, float, float]:
    """``(token_set_ratio, token_sort_ratio, their average)``, each 0..100, of two already-normalised strings."""
    set_ratio = token_set_ratio(left, right)
    sort_ratio = token_sort_ratio(left, right)
    return set_ratio, sort_ratio, (set_ratio + sort_ratio) / 2


def fuzzy_matrix(queries: Sequence[str | None], choices: Sequence[str | None]) -> np.ndarray:
    """``(token_set_ratio + token_sort_ratio) / 2 / 100`` of every query against every choice, as one
    ``(queries, choices)`` float32 matrix. Missing strings score 0. Two C++ calls for the whole batch instead of
    two Python calls per pair."""
    if not len(queries) or not len(choices):
        return np.zeros((len(queries), len(choices)), dtype=np.float32)
    from rapidfuzz import process

    fuzz = _fuzz()
    left = [q or "" for q in queries]
    right = [c or "" for c in choices]
    by_set = process.cdist(left, right, scorer=fuzz.token_set_ratio, dtype=np.float32, workers=-1)
    by_sort = process.cdist(left, right, scorer=fuzz.token_sort_ratio, dtype=np.float32, workers=-1)
    matrix = (by_set + by_sort) / 200.0
    # Two empty strings "match" perfectly in rapidfuzz; a missing description matches nothing.
    matrix[[not q for q in queries], :] = 0.0
    matrix[:, [not c for c in choices]] = 0.0
    return matrix


def combine(
    cosine: Decimal, ratio: float, embedding_weight: Decimal, fuzzy_weight: Decimal
) -> Decimal:
    """``w_e x cosine + w_f x ratio / 100``, to six places, clamped to [0, 1]. ``ratio`` is the fuzzy average
    (``fuzzy_ratio``'s third value), 0..100."""
    value = embedding_weight * cosine + fuzzy_weight * Decimal(str(round(ratio, 6))) / 100
    value = min(max(value, Decimal(0)), Decimal(1))
    return value.quantize(Decimal("0.000001"))
