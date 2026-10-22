"""Description embeddings kept in Azure SQL between OAR runs (``i7_oar_embedding``).

The store behind ``text_similarity.EmbeddingCache``. A run looks up every
description's vector here before calling the model, so a second run over an
unchanged catalogue generates no embedding at all -- and a run after a handful
of descriptions changed generates exactly those.

STALENESS IS IMPOSSIBLE BY CONSTRUCTION, NOT BY HOUSEKEEPING

The key (``text_similarity.cache_key``) hashes the normalised description
together with the model name, the model version and the preprocessing version.
A description edited in SAP, a new model, or a changed normalisation rule each
produce a key that has never been stored, so the old vector is not wrong -- it
is simply never asked for again. :func:`SqlEmbeddingStore.prune_other_versions`
and :func:`SqlEmbeddingStore.clear` reclaim the space; neither is needed for
correctness, and ``clear`` is also the full rebuild: the next run regenerates
whatever it needs.

Why the database and not a file or a cache service: the vectors are small
(3 KB each at 768 float32 dimensions), the database is already the one store
every worker and every instance shares, and it needs no new Azure resource.
Redis or a vector database would add a service to buy nothing at this volume.

Reads and writes are chunked so no statement exceeds SQL Server's 2,100
bind-parameter ceiling, and nothing here holds more than one chunk of rows
beyond the vectors it returns.
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence

import numpy as np
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.initiatives.i7.oar.text_similarity import PREPROCESSING_VERSION, StoredEmbedding
from app.models.i7_oar import OarEmbedding

logger = logging.getLogger(__name__)

CHUNK = 500
"""Keys per SELECT, rows per INSERT batch. Well under SQL Server's 2,100
bind-parameter limit, which a single wide INSERT would otherwise hit."""


def to_bytes(vector: np.ndarray) -> bytes:
    """Little-endian float32 -- explicit, so the bytes mean the same on any host."""
    return np.asarray(vector, dtype="<f4").tobytes()


def from_bytes(raw: bytes, dimensions: int) -> np.ndarray:
    vector = np.frombuffer(raw, dtype="<f4")
    if vector.shape[0] != dimensions:
        raise ValueError(
            f"stored embedding has {vector.shape[0]} values, expected {dimensions}"
        )
    return vector.astype(np.float32)


class SqlEmbeddingStore:
    """``text_similarity.EmbeddingStore`` over ``i7_oar_embedding``."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get_many(self, keys: Sequence[str]) -> dict[str, np.ndarray]:
        found: dict[str, np.ndarray] = {}
        keys = list(dict.fromkeys(keys))
        with self._session_factory() as session:
            for start in range(0, len(keys), CHUNK):
                chunk = keys[start : start + CHUNK]
                rows = session.execute(
                    select(OarEmbedding.cache_key, OarEmbedding.vector, OarEmbedding.dimensions)
                    .where(OarEmbedding.cache_key.in_(chunk))
                ).all()
                for key, raw, dimensions in rows:
                    found[key] = from_bytes(raw, dimensions)
        return found

    def put_many(self, entries: Sequence[StoredEmbedding]) -> None:
        """Store vectors whose key is not yet stored. Idempotent.

        OAR runs are serialised by the I07 pipeline lock, so two writers racing
        for the same key is unexpected -- but a hand-run beside the pipeline
        could do it, and a duplicate key is not an error worth failing a run
        for: the vector would be identical. A batch that collides is retried row
        by row, skipping the keys that already exist.
        """
        if not entries:
            return
        unique = list({entry.key: entry for entry in entries}.values())
        existing = set(self.get_many([entry.key for entry in unique]))
        rows = [
            {
                "cache_key": entry.key,
                "model_name": entry.model_name,
                "model_version": entry.model_version,
                "preprocessing_version": PREPROCESSING_VERSION,
                "text_hash": entry.text_hash,
                "dimensions": int(np.asarray(entry.vector).shape[0]),
                "vector": to_bytes(entry.vector),
            }
            for entry in unique
            if entry.key not in existing
        ]
        for start in range(0, len(rows), CHUNK):
            chunk = rows[start : start + CHUNK]
            with self._session_factory() as session:
                try:
                    session.bulk_insert_mappings(OarEmbedding, chunk)
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    self._insert_one_by_one(chunk)

    def _insert_one_by_one(self, rows: list[dict]) -> None:
        for row in rows:
            with self._session_factory() as session:
                try:
                    session.bulk_insert_mappings(OarEmbedding, [row])
                    session.commit()
                except IntegrityError:
                    session.rollback()  # another writer stored it first

    def count(self) -> int:
        with self._session_factory() as session:
            return int(session.execute(select(func.count()).select_from(OarEmbedding)).scalar() or 0)

    def prune_other_versions(self, model_name: str, model_version: str) -> int:
        """Delete vectors from any other model, model version or preprocessing
        version. Space only -- they could never be looked up again anyway."""
        with self._session_factory() as session:
            stale = session.execute(
                # Plain OR rather than a row-value comparison: T-SQL has no
                # (a, b, c) <> (x, y, z).
                select(OarEmbedding.cache_key).where(
                    or_(
                        OarEmbedding.model_name != model_name,
                        OarEmbedding.model_version != model_version,
                        OarEmbedding.preprocessing_version != PREPROCESSING_VERSION,
                    )
                )
            ).scalars().all()
            for start in range(0, len(stale), CHUNK):
                session.execute(
                    delete(OarEmbedding).where(
                        OarEmbedding.cache_key.in_(stale[start : start + CHUNK])
                    )
                )
            session.commit()
        if stale:
            logger.info("OAR embeddings: pruned %d vector(s) from other versions", len(stale))
        return len(stale)

    def clear(self) -> int:
        """Delete every stored vector -- the full rebuild. The next run
        regenerates what it needs."""
        with self._session_factory() as session:
            deleted = session.execute(delete(OarEmbedding)).rowcount or 0
            session.commit()
        return deleted
