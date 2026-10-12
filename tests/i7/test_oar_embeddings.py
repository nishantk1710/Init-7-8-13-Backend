"""OAR description embeddings: computed once, kept, never stale.

No model is downloaded: the providers here are fakes that count what they are
asked to encode, which is exactly what these tests need to see. The SQL store
runs against in-memory SQLite with the real ``i7_oar_embedding`` model.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.oar import embedding_store, text_similarity
from app.initiatives.i7.oar.embedding_store import SqlEmbeddingStore
from app.initiatives.i7.oar.service import prepare_embeddings
from app.initiatives.i7.oar.text_similarity import (
    EmbeddingCache,
    InMemoryEmbeddingStore,
    MiniLmEmbeddingProvider,
    PrecomputedEmbeddingProvider,
    cache_key,
)
from app.models.base import Base
from app.models.i7_oar import OarEmbedding


class CountingProvider:
    """Deterministic vectors, and a record of every batch it was asked for."""

    model_name = "fake-model"

    def __init__(self, model_version: str = "v1", available: bool = True) -> None:
        self.model_version = model_version
        self.is_available = available
        self.batches: list[list[str]] = []

    def encode(self, texts):
        if not self.is_available:
            return None
        self.batches.append(list(texts))
        out = []
        for text in texts:
            seed = sum(map(ord, text + self.model_version))
            out.append(np.random.default_rng(seed).normal(size=8).astype(np.float32))
        return out

    @property
    def encoded(self) -> list[str]:
        return [text for batch in self.batches for text in batch]


# --- The key ----------------------------------------------------------------------


def test_the_key_changes_with_everything_that_changes_the_vector(monkeypatch):
    base = cache_key("ball bearing 6310", "all-MiniLM-L6-v2", "v1")
    assert cache_key("ball bearing 6311", "all-MiniLM-L6-v2", "v1") != base
    assert cache_key("ball bearing 6310", "other-model", "v1") != base
    assert cache_key("ball bearing 6310", "all-MiniLM-L6-v2", "v2") != base
    monkeypatch.setattr(text_similarity, "PREPROCESSING_VERSION", "ws-collapse-2")
    assert cache_key("ball bearing 6310", "all-MiniLM-L6-v2", "v1") != base


def test_the_key_is_deterministic():
    assert cache_key("x", "m", "v") == cache_key("x", "m", "v")
    assert len(cache_key("x", "m", "v")) == 64


# --- The cache --------------------------------------------------------------------


def test_each_unique_description_is_embedded_once():
    provider = CountingProvider()
    cache = EmbeddingCache(provider)

    vectors = cache.ensure(["pump seal", "pump  seal", " pump seal ", "valve", None, "   "])

    assert sorted(provider.encoded) == ["pump seal", "valve"]
    assert set(vectors) == {"pump seal", "valve"}
    assert cache.stats.unique == 2 and cache.stats.generated == 2


def test_a_repeated_run_generates_nothing():
    """The point of the store: the next run over the same catalogue embeds
    nothing, whether it shares the process (memory) or not (store)."""
    store = InMemoryEmbeddingStore()
    first = CountingProvider()
    EmbeddingCache(first, store=store).ensure(["a", "b", "c"])

    same_process = EmbeddingCache(first, store=store)
    same_process.ensure(["a", "b", "c"])
    later = CountingProvider()
    later_cache = EmbeddingCache(later, store=store)
    later_cache.ensure(["a", "b", "c"])

    assert later.encoded == []
    assert later_cache.stats.store_hits == 3
    assert later_cache.stats.hit_rate == 1.0


def test_a_changed_description_regenerates_only_that_description():
    store = InMemoryEmbeddingStore()
    EmbeddingCache(CountingProvider(), store=store).ensure(["a", "b", "c"])

    provider = CountingProvider()
    EmbeddingCache(provider, store=store).ensure(["a", "b", "c (revised)"])

    assert provider.encoded == ["c (revised)"]


def test_a_new_model_version_invalidates_every_old_vector():
    store = InMemoryEmbeddingStore()
    EmbeddingCache(CountingProvider("v1"), store=store).ensure(["a", "b"])

    upgraded = CountingProvider("v2")
    vectors = EmbeddingCache(upgraded, store=store).ensure(["a", "b"])

    assert sorted(upgraded.encoded) == ["a", "b"]
    old = EmbeddingCache(CountingProvider("v1"), store=store).ensure(["a"])
    assert not np.array_equal(vectors["a"], old["a"])  # the old vector is never served


def test_batches_are_bounded():
    provider = CountingProvider()
    EmbeddingCache(provider, batch_size=3).ensure([f"text {i}" for i in range(10)])
    assert [len(batch) for batch in provider.batches] == [3, 3, 3, 1]


def test_an_unavailable_model_gives_no_vectors_not_fake_ones():
    assert EmbeddingCache(CountingProvider(available=False)).ensure(["a"]) is None
    vectors, stats = prepare_embeddings(CountingProvider(available=False), ["a"])
    assert vectors is None and stats is None


def test_the_scoring_provider_never_calls_a_model():
    vectors = {"a": np.ones(4, dtype=np.float32)}
    provider = PrecomputedEmbeddingProvider(vectors)
    assert provider.encode(["a"])[0] is vectors["a"]
    with pytest.raises(KeyError):
        provider.encode(["never embedded"])  # a wiring fault, said loudly
    assert PrecomputedEmbeddingProvider(None).encode(["a"]) is None


# --- The real provider's loading behaviour -------------------------------------------


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    """A stand-in ``sentence_transformers`` module that counts model loads."""
    loads = {"n": 0}
    calls: list[int] = []

    class SentenceTransformer:
        def __init__(self, name):
            loads["n"] += 1
            self.name = name

        def encode(self, texts, batch_size, convert_to_numpy, show_progress_bar):
            calls.append(len(texts))
            return np.ones((len(texts), 4), dtype=np.float32)

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = SentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(text_similarity, "_MODELS", {})
    monkeypatch.setattr(text_similarity, "_MODEL_FAILURES", set())
    return loads, calls


def test_the_model_is_loaded_once_per_process(fake_sentence_transformers):
    loads, _ = fake_sentence_transformers
    for _ in range(3):
        provider = MiniLmEmbeddingProvider()
        assert provider.is_available
        provider.encode(["a", "b"])
    assert loads["n"] == 1


def test_the_real_provider_encodes_in_bounded_batches(fake_sentence_transformers):
    _, calls = fake_sentence_transformers
    MiniLmEmbeddingProvider(batch_size=4).encode([f"t{i}" for i in range(10)])
    assert calls == [4, 4, 2]


def test_a_missing_library_is_remembered_not_retried(monkeypatch):
    monkeypatch.setattr(text_similarity, "_MODELS", {})
    monkeypatch.setattr(text_similarity, "_MODEL_FAILURES", set())
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # import fails
    provider = MiniLmEmbeddingProvider()
    assert provider.is_available is False
    assert provider.encode(["a"]) is None
    assert text_similarity.MODEL_NAME in text_similarity._MODEL_FAILURES


# --- The SQL store --------------------------------------------------------------------


@pytest.fixture
def store():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[OarEmbedding.__table__])
    return SqlEmbeddingStore(sessionmaker(bind=engine))


def test_vectors_round_trip_exactly(store):
    provider = CountingProvider()
    first = EmbeddingCache(provider, store=store).ensure(["pump seal", "valve"])

    fresh = EmbeddingCache(CountingProvider(), store=store)
    again = fresh.ensure(["pump seal", "valve"])

    assert fresh.stats.store_hits == 2 and fresh.stats.generated == 0
    for text in first:
        assert again[text].dtype == np.float32
        assert np.array_equal(again[text], first[text])


def test_storing_twice_does_not_duplicate(store):
    entries = [
        text_similarity.StoredEmbedding(
            key=cache_key("a", "m", "v"), vector=np.ones(4, dtype=np.float32),
            model_name="m", model_version="v", text_hash="h",
        )
    ]
    store.put_many(entries)
    store.put_many(entries)
    assert store.count() == 1


def test_reads_and_writes_are_chunked(store, monkeypatch):
    """No statement may carry more keys than SQL Server's parameter ceiling."""
    monkeypatch.setattr(embedding_store, "CHUNK", 3)
    provider = CountingProvider()
    texts = [f"text {i}" for i in range(10)]
    EmbeddingCache(provider, store=store).ensure(texts)
    assert store.count() == 10
    assert len(store.get_many([cache_key(t, "fake-model", "v1") for t in texts])) == 10


def test_pruning_removes_only_other_versions(store):
    EmbeddingCache(CountingProvider("v1"), store=store).ensure(["a", "b"])
    EmbeddingCache(CountingProvider("v2"), store=store).ensure(["a"])

    removed = store.prune_other_versions("fake-model", "v2")

    assert removed == 2
    assert store.count() == 1


def test_clear_is_a_full_rebuild(store):
    EmbeddingCache(CountingProvider(), store=store).ensure(["a", "b"])
    assert store.clear() == 2
    provider = CountingProvider()
    EmbeddingCache(provider, store=store).ensure(["a", "b"])
    assert sorted(provider.encoded) == ["a", "b"]
