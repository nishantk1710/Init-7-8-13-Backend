"""``run_oar_similarity`` end to end, on in-memory SQLite.

The engine tests prove the answers; this proves the run around them: rows are
written in batches without losing any, every neighbour row carries its
explanation, the run records how candidates were found, an unchanged run is
reused, a forced re-run embeds nothing because the store already has every
vector, and the two retrieval modes persist the same results.

The repository loaders are replaced with fixtures -- they read the Phase 3
feature store, which is not what is under test here -- and so is the embedding
model, with a counting fake.
"""

from __future__ import annotations

import random
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import MetaData, create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.config as config_module
from app.core.config import Settings
from app.initiatives.i7.oar import repository, service
from app.initiatives.i7.oar.retrieval import RetrievalConfig
from app.initiatives.i7.oar.types import CandidateAttributes
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import SimilarityPolicy
from app.models.base import Base
from app.models.i7_features import FeatureBuildRun
from app.models.i7_inventory import InventoryRun
from app.models.i7_oar import OarEmbedding, OarNeighbour, OarRun, OarTargetResult

POLICY = PolicyDocument(
    service_level=load_mock_service_level_policy(),
    max_stock=load_mock_max_stock_policy(),
    similarity=SimilarityPolicy(minimum_similarity=0.30),
)
TEXTS = [
    "SKF 6310-2RS deep groove ball bearing", "ball bearing 6310 2RS", "pump seal kit",
    "mechanical seal kit pump", "gate valve DN100", "valve gate DN100 PN16", "motor 55kW",
]


class CountingProvider:
    model_name = "fake-model"
    model_version = "v1"
    is_available = True

    def __init__(self) -> None:
        self.encoded: list[str] = []

    def encode(self, texts):
        self.encoded.extend(texts)
        return [
            np.random.default_rng(sum(map(ord, t))).normal(size=8).astype(np.float32)
            for t in texts
        ]


def _attributes(rng: random.Random, number: str, donor: bool) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=number,
        sap_plant_code="1300",
        criticality=rng.choice(["NORMAL", "CRITICAL"]),
        is_active=True,
        history_months=24 if donor else 0,
        material_group=rng.choice(["BRG", "PMP", "VLV"]),
        equipment_type=None,
        base_unit_of_measure="EA",
        circuit=None,
        manufacturer=None,
        unit_price=Decimal(rng.randint(50, 500)),
        description=rng.choice(TEXTS),
    )


@pytest.fixture
def world(monkeypatch):
    rng = random.Random(7)
    donors = [_attributes(rng, f"D{i:04d}", True) for i in range(120)]
    targets = [_attributes(rng, f"T{i:04d}", False) for i in range(9)]
    inventory = {
        (d.sap_material_number, d.sap_plant_code): {"safety_stock": 3, "rop": 6, "max_stock": 15}
        for d in donors[::2]
    }

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[
            FeatureBuildRun.__table__, InventoryRun.__table__, OarRun.__table__,
            OarNeighbour.__table__, OarEmbedding.__table__,
        ],
    )
    # i7_oar_target cannot be built from the model as it stands on main: its
    # ``status`` column has index=True (auto-named ix_i7_oar_target_status) AND
    # __table_args__ declares an explicit Index with that same name, so any
    # create_all fails. Production builds the table through migrations and never
    # hits it. Pre-existing and out of scope here -- this builds the table
    # without the duplicate so the run can be tested.
    target_copy = OarTargetResult.__table__.to_metadata(MetaData())
    for index in list(target_copy.indexes):
        if index.name == "ix_i7_oar_target_status" and len(index.columns) == 2:
            target_copy.indexes.discard(index)
    target_copy.create(engine)
    factory = sessionmaker(bind=engine)

    monkeypatch.setattr(service, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(repository, "load_targets", lambda session: targets)
    monkeypatch.setattr(repository, "load_candidate_population", lambda session: donors)
    monkeypatch.setattr(repository, "load_inventory_values", lambda session: inventory)
    settings = Settings(
        i7_oar_write_batch_size=2,  # 9 targets -> several partial batches
        i7_oar_structured_pool=5,
        i7_oar_semantic_pool=5,
        i7_oar_fused_pool=15,
        i7_oar_exhaustive_below=0,
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    return factory, targets


def _target_rows(factory, run_id):
    with factory() as session:
        rows = session.execute(
            select(OarTargetResult).where(OarTargetResult.oar_run_id == run_id)
            .order_by(OarTargetResult.sap_material_number)
        ).scalars().all()
        return [
            (r.sap_material_number, r.status, r.confidence, r.eligible_candidates,
             r.neighbour_count, r.best_similarity, r.estimate_status, r.safety_stock,
             r.rop, r.max_stock, r.low_evidence_candidates)
            for r in rows
        ]


def _neighbour_rows(factory, run_id):
    with factory() as session:
        rows = session.execute(
            select(OarNeighbour).where(OarNeighbour.oar_run_id == run_id)
            .order_by(OarNeighbour.sap_material_number, OarNeighbour.rank)
        ).scalars().all()
        return [
            (r.sap_material_number, r.rank, r.neighbour_material, r.combined_similarity,
             r.score_explanation)
            for r in rows
        ]


def test_a_run_writes_every_target_and_explains_every_neighbour(world):
    factory, targets = world
    provider = CountingProvider()

    result = service.run_oar_similarity(POLICY, provider=provider)

    assert result.status == "succeeded", result.error
    assert result.targets_evaluated == len(targets)
    rows = _target_rows(factory, result.run_id)
    assert len(rows) == len(targets)
    neighbours = _neighbour_rows(factory, result.run_id)
    assert neighbours, "no neighbours at all -- the fixture proves nothing"
    assert all(row[4] and row[4].startswith("used=") for row in neighbours)
    with factory() as session:
        run = session.get(OarRun, result.run_id)
        assert run.retrieval_config.startswith("mode=two_stage")
        assert run.algorithm_version == "oar-similarity-6"
        shortlisted = session.execute(
            select(OarTargetResult.shortlisted_candidates).where(OarTargetResult.oar_run_id == result.run_id)
        ).scalars().all()
        assert all(n is not None for n in shortlisted)
    assert 0 < result.scored_fraction < 1


def test_embeddings_are_kept_and_a_forced_rerun_generates_none(world):
    factory, _ = world
    first_provider = CountingProvider()
    first = service.run_oar_similarity(POLICY, provider=first_provider)
    with factory() as session:
        stored = session.execute(select(func.count()).select_from(OarEmbedding)).scalar()
    assert stored == len(set(TEXTS))
    assert first.embeddings_generated == len(set(TEXTS))

    rerun_provider = CountingProvider()
    rerun = service.run_oar_similarity(POLICY, provider=rerun_provider, force=True)

    assert rerun.run_id != first.run_id
    assert rerun_provider.encoded == []
    assert rerun.embeddings_generated == 0
    assert rerun.embedding_cache_hit_rate == 1.0
    assert _target_rows(factory, rerun.run_id) == _target_rows(factory, first.run_id)


def test_an_unchanged_run_is_reused(world):
    first = service.run_oar_similarity(POLICY, provider=CountingProvider())
    again = service.run_oar_similarity(POLICY, provider=CountingProvider())
    assert again.reused_existing is True
    assert again.run_id == first.run_id


def test_exhaustive_and_two_stage_runs_persist_the_same_results(world):
    factory, _ = world
    two_stage = service.run_oar_similarity(POLICY, provider=CountingProvider())
    exhaustive = service.run_oar_similarity(
        POLICY, provider=CountingProvider(), config=RetrievalConfig(mode="exhaustive")
    )

    assert exhaustive.run_id != two_stage.run_id  # a different retrieval is a different run
    assert _target_rows(factory, exhaustive.run_id) == _target_rows(factory, two_stage.run_id)
    assert _neighbour_rows(factory, exhaustive.run_id) == _neighbour_rows(factory, two_stage.run_id)
    assert exhaustive.scored_fraction == 1.0


def test_without_a_model_the_run_records_no_text_similarity(world):
    factory, _ = world

    class NoModel(CountingProvider):
        is_available = False

    result = service.run_oar_similarity(POLICY, provider=NoModel())

    assert result.status == "succeeded"
    assert result.text_similarity_available is False
    with factory() as session:
        run = session.get(OarRun, result.run_id)
        assert run.embedding_model_version == "none"
        assert session.execute(select(func.count()).select_from(OarEmbedding)).scalar() == 0
    explanations = [row[4] for row in _neighbour_rows(factory, result.run_id)]
    assert explanations and all("text=NOT_AVAILABLE_NO_MODEL" in e for e in explanations)
