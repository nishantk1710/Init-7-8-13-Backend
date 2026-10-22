"""Dimension 2 -- material description text similarity.

Two signals over normalised descriptions (``oar.text_match``)::

    encode with BAAI/bge-base-en-v1.5 (768-dim, normalised vectors, CPU)
    S_text = 0.6 x cosine(embed_a, embed_b)  +  0.4 x (token_set_ratio + token_sort_ratio) / 2 / 100

The Solution Design named all-MiniLM-L6-v2 and the cosine alone; ``oar-similarity-11`` replaced both (BGE for
the meaning, rapidfuzz for the shared words) and MiniLM is no longer supported. The weights are
``SimilarityPolicy.text_embedding_weight`` / ``text_fuzzy_weight``.

**The provider is an interface with one real implementation.**
``BgeEmbeddingProvider`` loads exactly the specified model; nothing
substitutes TF-IDF, token overlap or another transformer when it is absent. If
``sentence-transformers`` or ``rapidfuzz`` is not installed or the model cannot be loaded, the
dimension reports ``NOT_AVAILABLE_NO_MODEL`` and the combined score renormalises
over the dimensions that did compute. Installing the libraries later switches this
on with no code change. They are deliberately NOT in requirements.txt: sentence-transformers pulls in
torch (see requirements-oar-text.txt), and the engine is complete without them.

Tests inject a deterministic fake provider, so the suite never downloads a model.

**An embedding is computed once per description, not once per comparison.**
Three layers sit in front of the model, cheapest first:

1. the per-run map built by :class:`EmbeddingCache` -- one vector per unique
   normalised description, however many targets and donors share it;
2. an :class:`EmbeddingStore` that survives the process (``oar.embedding_store``
   keeps it in Azure SQL), so a second OAR run over unchanged descriptions
   generates nothing;
3. the model, called in bounded batches only for what neither layer had.

Every layer is keyed by :func:`cache_key`, which covers the normalised text, the
model name, the model version and :data:`PREPROCESSING_VERSION`. A changed
description, a different model or a changed preprocessing rule therefore
produces a different key: the old vector is simply never looked up again, so a
stale embedding cannot be returned. Nothing has to be invalidated by hand.

**The model is loaded once per process.** Loading costs seconds; it is cached at
module level by model name, so a second run (or a second provider instance) in
the same worker reuses it.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Protocol, Sequence

import numpy as np

from app.initiatives.i7.oar import text_match
from app.initiatives.i7.oar.types import DimensionScore, SimilarityStatus

MODEL_NAME = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIMENSIONS = 768

PREPROCESSING_VERSION = "ws-collapse-1"
"""Names the rule in :func:`normalise_description`. Part of every cache key, so
changing the rule invalidates every stored embedding by construction. Bump it
whenever ``normalise_description`` changes what it returns."""

FEATURE = "description"

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
    """Hash of a description alone. Changing a description changes the hash, so
    a stale embedding is never returned for new text."""
    return hashlib.sha256(description.encode("utf-8")).hexdigest()[:32]


def cache_key(normalised_text: str, model_name: str, model_version: str) -> str:
    """The key an embedding is stored and looked up under.

    Covers everything that changes the vector: the text, the model, its version
    and the preprocessing rule. 64 hex characters of SHA-256.
    """
    material = "\x1f".join(
        (model_name or "", model_version or "", PREPROCESSING_VERSION, normalised_text)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class EmbeddingProvider(Protocol):
    """Encodes descriptions into vectors."""

    model_name: str
    model_version: str

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        """Embeddings in input order, or ``None`` if the model is unavailable."""
        ...


_MODELS: dict[str, object] = {}
_MODEL_FAILURES: set[str] = set()
_MODEL_LOCK = threading.Lock()


def _load_model(name: str):
    """The sentence-transformers model, loaded once per process and name.

    ``None`` if it cannot be loaded -- not installed, no cached weights and no
    network, or a load failure. A failure is remembered too, so every later call
    short-circuits instead of retrying an import that will not succeed.
    """
    with _MODEL_LOCK:
        if name in _MODELS:
            return _MODELS[name]
        if name in _MODEL_FAILURES:
            return None
        try:
            from sentence_transformers import SentenceTransformer

            _MODELS[name] = SentenceTransformer(name)
            return _MODELS[name]
        except Exception:
            _MODEL_FAILURES.add(name)
            return None


class BgeEmbeddingProvider:
    """The real provider: sentence-transformers loading BAAI/bge-base-en-v1.5, vectors normalised.

    Imported lazily inside :func:`_load_model` so that importing this module --
    which the whole OAR package does -- never requires the dependency. Absent
    the library, ``encode`` returns ``None`` and the caller reports the dimension
    as unavailable. ``rapidfuzz`` is checked here too: the text dimension is the
    embedding AND the fuzzy ratio, and the second half is not skipped silently.

    The text handed in is the whitespace-normalised description (what the embedding cache is keyed by); it is
    put through :func:`oar.text_match.normalise_for_match` (upper-case, commas removed, abbreviations expanded)
    just before it reaches the model. ``model_version`` carries the normalisation version, so changing those
    rules is a different cache key.

    ``batch_size`` bounds how many descriptions go to the model per call, which
    bounds the activation memory of one forward pass.
    """

    model_name = MODEL_NAME

    def __init__(self, model_version: str | None = None, batch_size: int = 64) -> None:
        self.model_version = model_version or f"{MODEL_NAME}+{text_match.NORMALISATION_VERSION}"
        self.batch_size = max(1, int(batch_size))
        self._cache: dict[str, list[float]] = {}

    def _model(self):
        return _load_model(self.model_name)

    @property
    def is_available(self) -> bool:
        return text_match.available() and self._model() is not None

    def encode(self, texts: list[str]) -> list[list[float]] | None:
        """Encode in bounded batches, reusing vectors this instance already has."""
        model = self._model()
        if model is None:
            return None

        pending = list(dict.fromkeys(t for t in texts if description_hash(t) not in self._cache))
        for start in range(0, len(pending), self.batch_size):
            chunk = pending[start : start + self.batch_size]
            vectors = model.encode(
                [text_match.normalise_for_match(t) or t for t in chunk],
                batch_size=self.batch_size,
                convert_to_numpy=True,
                show_progress_bar=False,
                normalize_embeddings=True,
            )
            for text, vector in zip(chunk, vectors):
                self._cache[description_hash(text)] = [float(value) for value in vector]

        return [self._cache[description_hash(text)] for text in texts]


def cosine(left: Sequence[float], right: Sequence[float]) -> Decimal | None:
    """Cosine similarity, clamped to [0, 1], to six decimal places.

    A zero-norm vector has no direction, so the similarity is undefined rather
    than zero. Embeddings can carry small negatives; the documented score
    is a similarity in [0, 1], so negatives floor at 0 -- "no resemblance"
    rather than "opposite", which is not a meaningful claim about spare parts.

    Computed in float64 with numpy -- the same answer as the pure-Python loop it
    replaced to within float rounding, at a fraction of the cost per pair, which
    matters when it runs millions of times in one OAR run.
    """
    if len(left) != len(right) or len(left) == 0:
        return None

    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    left_norm = math.sqrt(float(np.dot(a, a)))
    right_norm = math.sqrt(float(np.dot(b, b)))

    if left_norm == 0 or right_norm == 0:
        return None

    value = float(np.dot(a, b)) / (left_norm * right_norm)
    return Decimal(str(round(min(max(value, 0.0), 1.0), 6)))


def score(
    target_description: str | None,
    candidate_description: str | None,
    provider: EmbeddingProvider,
    embedding_weight: Decimal = Decimal(1),
    fuzzy_weight: Decimal = Decimal(0),
) -> DimensionScore:
    """Text similarity between two material descriptions.

    ``embedding_weight`` x cosine + ``fuzzy_weight`` x (token_set_ratio + token_sort_ratio) / 2 / 100. The
    defaults (1, 0) are the cosine alone, which is what a caller with no policy gets; the OAR run passes the
    policy's 0.6 / 0.4. Every part is kept on ``DimensionScore.components`` (``embedding``, ``fuzzy_set``,
    ``fuzzy_sort``) so a score can be explained.
    """
    target_text = normalise_description(target_description)
    candidate_text = normalise_description(candidate_description)

    if target_text is None or candidate_text is None:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_TEXT,
            missing_features=1,
            missing=(FEATURE,),
        )

    vectors = provider.encode([target_text, candidate_text])
    if vectors is None:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_MODEL,
            missing_features=1,
            missing=(FEATURE,),
        )

    similarity = cosine(vectors[0], vectors[1])
    if similarity is None:
        return DimensionScore(
            value=None,
            status=SimilarityStatus.NOT_AVAILABLE_NO_TEXT,
            missing_features=1,
            missing=(FEATURE,),
        )

    if fuzzy_weight == 0:
        return DimensionScore(
            value=similarity * embedding_weight,
            status=SimilarityStatus.AVAILABLE,
            available_features=1,
            features=(FEATURE,),
            components=(("embedding", str(similarity)),),
        )

    set_ratio, sort_ratio, ratio = text_match.fuzzy_ratio(
        text_match.normalise_for_match(target_text), text_match.normalise_for_match(candidate_text)
    )
    return DimensionScore(
        value=text_match.combine(similarity, ratio, embedding_weight, fuzzy_weight),
        status=SimilarityStatus.AVAILABLE,
        available_features=1,
        features=(FEATURE,),
        components=(
            ("embedding", str(similarity)),
            ("fuzzy_set", f"{set_ratio / 100:.4f}"),
            ("fuzzy_sort", f"{sort_ratio / 100:.4f}"),
        ),
    )


# --- Caching ----------------------------------------------------------------


class EmbeddingStore(Protocol):
    """Durable storage for vectors, keyed by :func:`cache_key`."""

    def get_many(self, keys: Sequence[str]) -> dict[str, np.ndarray]:
        """The stored vectors for whichever of ``keys`` exist."""
        ...

    def put_many(self, entries: Sequence["StoredEmbedding"]) -> None:
        """Store vectors not already stored. Existing keys are left alone."""
        ...


@dataclass(frozen=True)
class StoredEmbedding:
    key: str
    vector: np.ndarray
    model_name: str
    model_version: str
    text_hash: str


class InMemoryEmbeddingStore:
    """An :class:`EmbeddingStore` that lives as long as the object. For tests and
    for runs where persistence is switched off."""

    def __init__(self) -> None:
        self.vectors: dict[str, np.ndarray] = {}

    def get_many(self, keys: Sequence[str]) -> dict[str, np.ndarray]:
        return {key: self.vectors[key] for key in keys if key in self.vectors}

    def put_many(self, entries: Sequence[StoredEmbedding]) -> None:
        for entry in entries:
            self.vectors.setdefault(entry.key, entry.vector)


@dataclass
class EmbeddingStats:
    """What one :meth:`EmbeddingCache.ensure` call did, for the run log."""

    requested: int = 0
    unique: int = 0
    memory_hits: int = 0
    store_hits: int = 0
    generated: int = 0
    batches: int = 0

    @property
    def hit_rate(self) -> float:
        if not self.unique:
            return 1.0
        return (self.memory_hits + self.store_hits) / self.unique


class EmbeddingCache:
    """One vector per unique normalised description: memory, then store, then model.

    ``ensure`` returns a map from normalised text to a float32 vector. Missing
    descriptions are skipped (they have no text to embed). If the model is
    unavailable the result is ``None`` -- the caller reports the text dimension
    as ``NOT_AVAILABLE_NO_MODEL``, never as a score.
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        store: EmbeddingStore | None = None,
        batch_size: int = 256,
    ) -> None:
        self.provider = provider
        self.store = store or InMemoryEmbeddingStore()
        self.batch_size = max(1, int(batch_size))
        self._memory: dict[str, np.ndarray] = {}
        self.stats = EmbeddingStats()

    def _key(self, text: str) -> str:
        return cache_key(
            text,
            getattr(self.provider, "model_name", "") or "",
            getattr(self.provider, "model_version", "") or "",
        )

    def ensure(self, descriptions: Iterable[str | None]) -> dict[str, np.ndarray] | None:
        stats = EmbeddingStats()
        texts: list[str] = []
        for description in descriptions:
            stats.requested += 1
            text = normalise_description(description)
            if text is not None:
                texts.append(text)
        unique = list(dict.fromkeys(texts))
        stats.unique = len(unique)

        keys = {text: self._key(text) for text in unique}
        result: dict[str, np.ndarray] = {}

        missing_memory: list[str] = []
        for text in unique:
            vector = self._memory.get(keys[text])
            if vector is None:
                missing_memory.append(text)
            else:
                result[text] = vector
                stats.memory_hits += 1

        stored: dict[str, np.ndarray] = {}
        if missing_memory:
            stored = self.store.get_many([keys[text] for text in missing_memory])
        to_generate: list[str] = []
        for text in missing_memory:
            vector = stored.get(keys[text])
            if vector is None:
                to_generate.append(text)
            else:
                vector = np.asarray(vector, dtype=np.float32)
                self._memory[keys[text]] = vector
                result[text] = vector
                stats.store_hits += 1

        for start in range(0, len(to_generate), self.batch_size):
            chunk = to_generate[start : start + self.batch_size]
            vectors = self.provider.encode(chunk)
            stats.batches += 1
            if vectors is None:
                self.stats = stats
                return None
            new_entries: list[StoredEmbedding] = []
            for text, raw in zip(chunk, vectors):
                vector = np.asarray(raw, dtype=np.float32)
                key = keys[text]
                self._memory[key] = vector
                result[text] = vector
                new_entries.append(
                    StoredEmbedding(
                        key=key,
                        vector=vector,
                        model_name=getattr(self.provider, "model_name", "") or "",
                        model_version=getattr(self.provider, "model_version", "") or "",
                        text_hash=description_hash(text),
                    )
                )
            self.store.put_many(new_entries)
            stats.generated += len(chunk)

        self.stats = stats
        return result


class PrecomputedEmbeddingProvider:
    """A provider over vectors already computed for this run.

    What scoring uses during a run: it never calls a model, so exact scoring of
    a shortlist costs a dictionary lookup per description. ``vectors`` of
    ``None`` means the model was unavailable, and ``encode`` then returns
    ``None`` exactly as the real provider would.
    """

    def __init__(
        self,
        vectors: dict[str, np.ndarray] | None,
        model_name: str = MODEL_NAME,
        model_version: str = MODEL_NAME,
    ) -> None:
        self._vectors = vectors
        self.model_name = model_name
        self.model_version = model_version

    @property
    def is_available(self) -> bool:
        return self._vectors is not None

    def vector(self, text: str) -> np.ndarray | None:
        if self._vectors is None:
            return None
        return self._vectors.get(text)

    def encode(self, texts: list[str]) -> list[np.ndarray] | None:
        if self._vectors is None:
            return None
        out = []
        for text in texts:
            vector = self._vectors.get(text)
            if vector is None:
                # A description that reached scoring without an embedding is a
                # wiring fault, not a "no text" case -- say so loudly.
                raise KeyError(f"no precomputed embedding for description {text[:60]!r}")
            out.append(vector)
        return out
