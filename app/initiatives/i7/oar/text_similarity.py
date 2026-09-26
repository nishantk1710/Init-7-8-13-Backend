"""Dimension 2 -- material description embeddings.

Solution Design, Stage 5::

    encode with all-MiniLM-L6-v2 (384-dim, CPU)
    S_text = cosine(embed_a, embed_b)

**The provider is an interface with one real implementation.**
``MiniLmEmbeddingProvider`` loads exactly the specified model; nothing
substitutes TF-IDF, token overlap or another transformer when it is absent. If
``sentence-transformers`` is not installed or the model cannot be loaded, the
dimension reports ``NOT_AVAILABLE_NO_MODEL`` and the combined score renormalises
over the dimensions that did compute. Installing the library later switches this
on with no code change.

Tests inject a deterministic fake provider, so the suite never downloads a model.

**Loaded once, cached per process.** Encoding is the expensive step, and the
model itself costs seconds to load -- doing either per candidate would dominate
the run. Embeddings are cached by description hash, because the same description
is compared against many targets.
"""

import hashlib
import math
import re
from decimal import Decimal
from typing import Protocol

from app.initiatives.i7.oar.types import DimensionScore, SimilarityStatus

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384

_WHITESPACE = re.compile(r"\s+")


def normalise_description(description: str | None) -> str | None:
    """Conservative, deterministic preprocessing.

    Trims and collapses whitespace, nothing more. Technical identifiers --
    ``6310-2RS``, ``55KW`` -- carry most of the discriminating meaning in a
    spare-part description, so no stop-word removal, stemming or case folding is
    applied; those would erase exactly the tokens that distinguish one bearing
    from another.
    """
    if description is None:
        return None
    collapsed = _WHITESPACE.sub(" ", description).strip()
    return collapsed or None


def description_hash(description: str) -> str:
    """Cache key. Changing a description changes the hash, so a stale embedding
    is never returned for new text."""
    return hashlib.sha256(description.encode("utf-8")).hexdigest()[:32]


class EmbeddingProvider(Protocol):
    """Encodes descriptions into vectors."""

    model_name: str
    model_version: str

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        """Embeddings in input order, or ``None`` if the model is unavailable."""
        ...


class MiniLmEmbeddingProvider:
    """The real provider: sentence-transformers loading all-MiniLM-L6-v2.

    Imported lazily inside :meth:`_model` so that importing this module -- which
    the whole OAR package does -- never requires the dependency. Absent the
    library, ``encode`` returns ``None`` and the caller reports the dimension as
    unavailable.
    """

    model_name = MODEL_NAME

    def __init__(self, model_version: str = MODEL_NAME) -> None:
        self.model_version = model_version
        self._loaded = None
        self._load_failed = False
        self._cache: dict[str, list[float]] = {}

    def _model(self):
        """The model, loaded once. ``None`` if it cannot be loaded."""
        if self._loaded is not None or self._load_failed:
            return self._loaded
        try:
            from sentence_transformers import SentenceTransformer

            self._loaded = SentenceTransformer(MODEL_NAME)
        except Exception:
            # Not installed, no cached model and no network, or a load failure.
            # Recorded once so every later call short-circuits rather than
            # retrying an import that will not succeed.
            self._load_failed = True
            self._loaded = None
        return self._loaded

    @property
    def is_available(self) -> bool:
        return self._model() is not None

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        """Encode in one batch, reusing cached vectors where possible."""
        model = self._model()
        if model is None:
            return None

        pending = [text for text in texts if description_hash(text) not in self._cache]
        if pending:
            unique = list(dict.fromkeys(pending))
            vectors = model.encode(unique, convert_to_numpy=True, show_progress_bar=False)
            for text, vector in zip(unique, vectors):
                self._cache[description_hash(text)] = [float(value) for value in vector]

        return [self._cache[description_hash(text)] for text in texts]


def cosine(left: list[float], right: list[float]) -> Decimal | None:
    """Cosine similarity, clamped to [0, 1].

    A zero-norm vector has no direction, so the similarity is undefined rather
    than zero. MiniLM embeddings can carry small negatives; the documented score
    is a similarity in [0, 1], so negatives floor at 0 -- "no resemblance"
    rather than "opposite", which is not a meaningful claim about spare parts.
    """
    if len(left) != len(right) or not left:
        return None

    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))

    if left_norm == 0 or right_norm == 0:
        return None

    value = dot / (left_norm * right_norm)
    return Decimal(str(round(min(max(value, 0.0), 1.0), 6)))


def score(
    target_description: str | None,
    candidate_description: str | None,
    provider: EmbeddingProvider,
) -> DimensionScore:
    """Cosine similarity between two material descriptions."""
    target_text = normalise_description(target_description)
    candidate_text = normalise_description(candidate_description)

    if target_text is None or candidate_text is None:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_TEXT,
            missing_features=1,
        )

    vectors = provider.encode([target_text, candidate_text])
    if vectors is None:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_MODEL,
            missing_features=1,
        )

    similarity = cosine(vectors[0], vectors[1])
    if similarity is None:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_TEXT,
            missing_features=1,
        )

    return DimensionScore(
        value=similarity, status=SimilarityStatus.AVAILABLE, available_features=1
    )
