"""A user removes a similar material from one target: persistence, builds, recalculation, approval.

Runs the real pipeline (``run_oar_similarity`` -> engine -> ``finalise_target``), the real single-target
refresh and the real recommendation builder on in-memory SQLite. Only the feature-store readers that
feed the *population* are replaced with fixtures (as in ``test_oar_run.py``) and the embedding model
with a deterministic fake.

What is held fixed throughout: the estimator, scoring and ranking are never reimplemented here -- the
expected answers come from calling ``service.evaluate_target`` (the exhaustive reference) with the same
exclusion, so a test can only pass if the refresh and the batch path agree with the unchanged pipeline.
"""

from __future__ import annotations

import random
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.config as config_module
from app.api.i7.recommendations import get_embedding_provider
from app.core.ai import AIError
from app.core.config import Settings
from app.core.db import get_db
from app.initiatives.i7.oar import exclusions, refresh, repository, service
from app.initiatives.i7.oar.retrieval import RetrievalConfig
from app.initiatives.i7.oar.text_similarity import PrecomputedEmbeddingProvider
from app.initiatives.i7.oar.types import CandidateAttributes, EstimateStatus
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.policy.thresholds import SimilarityPolicy
from app.initiatives.i7.recommendations import builder, oar_exclusions, rationale
from app.initiatives.i7.recommendations import repository as recommendation_repository
from app.initiatives.i7.recommendations import service as recommendation_service
from app.main import app
from app.models.base import Base
from app.models.i7_features import FeatureBuildRun, MaterialFeature
from app.models.i7_inventory import InventoryRun
from app.models.i7_oar import (
    OarDonorExclusion,
    OarEmbedding,
    OarNeighbour,
    OarRun,
    OarTargetResult,
)
from app.models.i7_recommendation import Recommendation, RecommendationVersion
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant

POLICY = PolicyDocument(
    service_level=load_mock_service_level_policy(),
    max_stock=load_mock_max_stock_policy(),
    similarity=SimilarityPolicy(minimum_similarity=0.30),
)
TEXTS = [
    "SKF 6310-2RS deep groove ball bearing", "ball bearing 6310 2RS", "pump seal kit",
    "mechanical seal kit pump", "gate valve DN100", "valve gate DN100 PN16", "motor 55kW",
]
TWO_STAGE = dict(
    i7_oar_write_batch_size=2,
    i7_oar_structured_pool=5,
    i7_oar_semantic_pool=5,
    i7_oar_fused_pool=15,
    i7_oar_exhaustive_below=0,
)


class FakeProvider:
    model_name = "fake-model"
    model_version = "v1"
    is_available = True

    def encode(self, texts):
        return [
            np.random.default_rng(sum(map(ord, t))).normal(size=8).astype(np.float32)
            for t in texts
        ]


def _attributes(rng: random.Random, number: str, donor: bool, *, uniform: bool = False) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=number,
        sap_plant_code="1300",
        criticality="NORMAL" if uniform else rng.choice(["NORMAL", "CRITICAL"]),
        is_active=True,
        history_months=24 if donor else 0,
        material_group="BRG" if uniform else rng.choice(["BRG", "PMP", "VLV"]),
        equipment_type=None,
        base_unit_of_measure="EA",
        circuit=None,
        manufacturer=None,
        unit_price=Decimal(100 if uniform else rng.randint(50, 500)),
        description=TEXTS[0] if uniform else rng.choice(TEXTS),
    )


def build_world(monkeypatch, *, donors: int, targets: int, uniform: bool = False, settings_overrides=None):
    rng = random.Random(7)
    donor_rows = [_attributes(rng, f"D{i:04d}", True, uniform=uniform) for i in range(donors)]
    target_rows = [_attributes(rng, f"T{i:04d}", False, uniform=uniform) for i in range(targets)]
    inventory = {
        (d.sap_material_number, d.sap_plant_code): {
            "safety_stock": 3 + i % 4, "rop": 6 + i % 5, "max_stock": 15 + i % 7,
        }
        for i, d in enumerate(donor_rows)
    }

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            FeatureBuildRun.__table__, InventoryRun.__table__, OarRun.__table__, OarNeighbour.__table__,
            OarEmbedding.__table__, OarDonorExclusion.__table__, MaterialFeature.__table__,
            RecommendationVersion.__table__, StagedMaterial.__table__, StagedMaterialPlant.__table__,
        ],
    )
    # i7_oar_target and i7_recommendation each declare an index twice -- once through index=True on the
    # column, once explicitly (see test_oar_run.py). Production builds them through migrations and never
    # hits it; build these two without the duplicate.
    for table in (OarTargetResult.__table__, Recommendation.__table__):
        copy = table.to_metadata(MetaData())
        seen: set[str] = set()
        for index in sorted(copy.indexes, key=lambda i: len(i.columns)):
            if index.name in seen:
                copy.indexes.discard(index)
            seen.add(index.name)
        copy.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    settings = Settings(**(settings_overrides or TWO_STAGE))
    for module in (service, refresh):
        monkeypatch.setattr(module, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(repository, "load_targets", lambda session: target_rows)
    monkeypatch.setattr(
        repository,
        "load_targets_for",
        lambda session, keys: [
            t for t in target_rows if (t.sap_material_number, t.sap_plant_code) in set(keys)
        ],
    )
    monkeypatch.setattr(repository, "load_candidate_population", lambda session: donor_rows)
    monkeypatch.setattr(repository, "load_inventory_values", lambda session: inventory)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(oar_exclusions, "get_settings", lambda: settings)

    real_load_oar_inputs = recommendation_repository.load_oar_inputs

    def load_oar_inputs_as_decimals(session, oar_run_id, target=None):
        # Raw text() SQL hands SQLite ints and floats where SQL Server hands Decimals for the feature
        # store's numeric columns; the builder's arithmetic is written for the latter. Same rows, same
        # SQL -- only those columns' numeric type is restored.
        decimal_columns = {
            "current_safety_stock", "current_reorder_point", "current_maximum_stock", "unit_price",
            "total_demand", "mean_demand_all_periods", "std_dev_demand_all_periods",
            "lead_time_days", "planned_delivery_time_days",
        }
        return [
            SimpleNamespace(
                **{
                    k: Decimal(str(v)) if k in decimal_columns and v is not None else v
                    for k, v in row._mapping.items()
                }
            )
            for row in real_load_oar_inputs(session, oar_run_id, target)
        ]

    monkeypatch.setattr(recommendation_repository, "load_oar_inputs", load_oar_inputs_as_decimals)

    # Nothing here may reach a real model: this checkout's .env configures one. With the AI call
    # refused, generate_rationale returns its own deterministic text, which is all these tests need.
    def no_ai_available(*args, **kwargs):
        raise AIError("AI is switched off in this test")

    monkeypatch.setattr(rationale, "complete_with_prompt", no_ai_available)
    return factory, target_rows, donor_rows, inventory, settings


@pytest.fixture
def world(monkeypatch):
    return build_world(monkeypatch, donors=120, targets=9)


def _neighbours(factory, run_id, target):
    with factory() as session:
        rows = session.execute(
            select(OarNeighbour)
            .where(OarNeighbour.oar_run_id == run_id, OarNeighbour.sap_material_number == target)
            .order_by(OarNeighbour.rank)
        ).scalars().all()
        return [(r.neighbour_material, r.neighbour_plant, r.rank, r.combined_similarity) for r in rows]


def _target_row(factory, run_id, target):
    with factory() as session:
        row = session.execute(
            select(OarTargetResult).where(
                OarTargetResult.oar_run_id == run_id, OarTargetResult.sap_material_number == target
            )
        ).scalar_one()
        return (
            row.neighbour_count, row.estimate_status, row.safety_stock, row.rop, row.max_stock,
            row.best_similarity,
        )


def _all_other_rows(factory, run_id, skip):
    with factory() as session:
        targets = [
            t for t in session.execute(
                select(OarTargetResult.sap_material_number).where(OarTargetResult.oar_run_id == run_id)
            ).scalars() if t != skip
        ]
    return {t: (_neighbours(factory, run_id, t), _target_row(factory, run_id, t)) for t in targets}


def _pick_target(factory, run_id, *, at_least=5):
    with factory() as session:
        counts = session.execute(
            select(OarTargetResult.sap_material_number, OarTargetResult.neighbour_count)
            .where(OarTargetResult.oar_run_id == run_id)
            .order_by(OarTargetResult.sap_material_number)
        ).all()
    for material, count in counts:
        if count >= at_least:
            return material
    pytest.fail(f"no target with {at_least} neighbours in the fixture: {counts}")


def _exclude_directly(factory, target, donor, plant="1300", by="seed"):
    with factory() as session:
        session.add(
            OarDonorExclusion(
                sap_material_number=target, sap_plant_code="1300", donor_material=donor,
                donor_plant=plant, created_by=by,
            )
        )
        session.commit()


def _seed_recommendation(factory, run_id, target, status="READY_FOR_REVIEW"):
    """A recommendation built by the production builder from the target's stored OAR result."""
    with factory() as session:
        session.add(
            MaterialFeature(
                sap_material_number=target, sap_plant_code="1300", feature_run_id=1,
                history_status="INSUFFICIENT", data_sufficiency="INSUFFICIENT",
                adi_status="NA", cv_squared_status="NA", oar_scope="IN_SCOPE", criticality="NORMAL",
                current_safety_stock=Decimal(4), current_reorder_point=Decimal(8),
                current_maximum_stock=Decimal(20), unit_price=Decimal(100), total_periods=3,
                non_zero_periods=2, consumption_count_12m=2, total_demand=Decimal(6),
            )
        )
        session.commit()
        rows = recommendation_repository.load_oar_inputs(session, run_id, target=(target, "1300"))
        built = builder.build_oar_recommendation(rows[0], POLICY)
        row = Recommendation(**recommendation_service._to_row(built, rationale.generate_rationale(built)))
        row.status = status
        session.add(row)
        session.commit()
        return row.recommendation_id


def _recommendation(factory, recommendation_id):
    with factory() as session:
        return session.execute(
            select(Recommendation).where(Recommendation.recommendation_id == recommendation_id)
        ).scalar_one()


def _run(provider=None, **kwargs):
    return service.run_oar_similarity(POLICY, provider=provider or FakeProvider(), **kwargs)


def _reference(world_, target_id, excluded):
    """The unchanged exhaustive pipeline's answer for one target, with these donors left out."""
    factory, targets, donors, inventory, _ = world_
    target = next(t for t in targets if t.sap_material_number == target_id)
    provider = FakeProvider()
    vectors, _ = service.prepare_embeddings(
        provider, [target.description] + [d.description for d in donors]
    )
    return service.evaluate_target(
        target, donors, PrecomputedEmbeddingProvider(vectors, provider.model_name, provider.model_version),
        repository.price_range(donors), inventory, POLICY, excluded=frozenset(excluded),
    )


def _exclude(world_, recommendation_id, donor, **kwargs):
    factory = world_[0]
    with factory() as session:
        row = session.execute(
            select(Recommendation).where(Recommendation.recommendation_id == recommendation_id)
        ).scalar_one()
        return oar_exclusions.exclude_donor(
            session, row, donor, "1300", actor_id=kwargs.pop("actor_id", "tester"),
            provider=FakeProvider(), policy=POLICY, **kwargs,
        )


def _restore(world_, recommendation_id, donor, actor_id="tester"):
    factory = world_[0]
    with factory() as session:
        row = session.execute(
            select(Recommendation).where(Recommendation.recommendation_id == recommendation_id)
        ).scalar_one()
        return oar_exclusions.restore_donor(
            session, row, donor, "1300", actor_id=actor_id, provider=FakeProvider(), policy=POLICY
        )


# --- The pure filter ---------------------------------------------------------------------------


def test_filter_scored_keeps_order_and_scores_and_drops_only_the_excluded_donor():
    entries = [("A", "1", "score-a", None, {}), ("B", "1", "score-b", None, {}), ("C", "2", "score-c", None, {})]
    kept = exclusions.filter_scored(entries, {("B", "1")})
    assert kept == [entries[0], entries[2]]
    assert kept[0] is entries[0]  # the very same entry, not rebuilt
    assert exclusions.filter_scored(entries, set()) == entries
    # only the exact material AND plant is dropped
    assert exclusions.filter_scored(entries, {("C", "1")}) == entries


# --- Persistence -------------------------------------------------------------------------------


def test_only_one_active_exclusion_per_target_and_donor_but_history_may_repeat(world):
    factory = world[0]
    _exclude_directly(factory, "T0000", "D0001")
    with pytest.raises(IntegrityError):
        _exclude_directly(factory, "T0000", "D0001")  # a second ACTIVE row for the same pair

    with factory() as session:
        first = session.execute(select(OarDonorExclusion)).scalar_one()
        first.active_slot = first.id  # what a restore does
        session.commit()
    _exclude_directly(factory, "T0000", "D0001")  # a restored row and a new active one coexist
    with factory() as session:
        assert session.execute(select(func.count()).select_from(OarDonorExclusion)).scalar() == 2
        assert exclusions.load_active(session) == {("T0000", "1300"): frozenset({("D0001", "1300")})}


# --- Future builds -----------------------------------------------------------------------------


def test_a_new_run_leaves_the_donor_out_and_the_next_best_takes_the_slot(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    before = _neighbours(factory, first.run_id, target)
    removed = before[1][0]  # not the top one, so the rest keep their relative order
    _exclude_directly(factory, target, removed)

    second = _run(force=True)

    after = _neighbours(factory, second.run_id, target)
    assert second.run_id != first.run_id
    assert removed not in [n[0] for n in after]
    assert len(after) == len(before)  # a replacement filled the slot
    assert [n[0] for n in after if n[0] in {b[0] for b in before}] == [
        b[0] for b in before if b[0] != removed
    ]  # survivors keep their order
    replacement = next(n for n in after if n[0] not in {b[0] for b in before})
    assert replacement[3] <= min(b[3] for b in before)  # it was ranked below every original neighbour
    # nobody else's result moved
    assert _all_other_rows(factory, second.run_id, target) == _all_other_rows(factory, first.run_id, target)


def test_exhaustive_and_two_stage_builds_apply_the_exclusion_identically(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    _exclude_directly(factory, target, _neighbours(factory, first.run_id, target)[0][0])

    two_stage = _run(force=True)
    exhaustive = _run(config=RetrievalConfig(mode="exhaustive"))

    assert _neighbours(factory, two_stage.run_id, target) == _neighbours(factory, exhaustive.run_id, target)
    assert _target_row(factory, two_stage.run_id, target) == _target_row(factory, exhaustive.run_id, target)


def test_a_reused_run_is_brought_up_to_date_for_the_excluded_target_only(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    before = _neighbours(factory, first.run_id, target)
    removed = before[0][0]
    others = _all_other_rows(factory, first.run_id, target)
    _exclude_directly(factory, target, removed)  # saved after the run, run inputs unchanged

    again = _run()

    assert again.reused_existing is True and again.run_id == first.run_id
    assert again.refreshed_targets == 1
    after = _neighbours(factory, first.run_id, target)
    assert removed not in [n[0] for n in after]
    assert len(after) == len(before)
    assert _all_other_rows(factory, first.run_id, target) == others
    assert _run().refreshed_targets == 0  # and it is not redone every time


def test_a_fresh_run_and_a_reused_run_give_the_same_target_result(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    removed = _neighbours(factory, first.run_id, target)[0][0]
    _exclude_directly(factory, target, removed)

    _run()  # reused + refreshed in place
    refreshed = (_neighbours(factory, first.run_id, target), _target_row(factory, first.run_id, target))
    fresh = _run(force=True)

    assert refreshed == (_neighbours(factory, fresh.run_id, target), _target_row(factory, fresh.run_id, target))


def test_the_refresh_falls_back_to_exhaustive_scoring_when_the_shortlist_runs_dry(monkeypatch):
    tiny = dict(TWO_STAGE, i7_oar_structured_pool=1, i7_oar_semantic_pool=1, i7_oar_fused_pool=5)
    world_ = build_world(monkeypatch, donors=120, targets=9, settings_overrides=tiny)
    factory, _, _, _, settings = world_
    first = _run()
    target = _pick_target(factory, first.run_id)
    # three of the five winners: the five-donor shortlist cannot refill the list from what it scored
    removed_donors = {(n[0], "1300") for n in _neighbours(factory, first.run_id, target)[:3]}

    with factory() as session:
        run = session.get(OarRun, first.run_id)
        results = refresh.compute(
            session, run, {(target, "1300"): frozenset(removed_donors)},
            policy=POLICY, provider=FakeProvider(), config=RetrievalConfig.from_settings(settings),
            settings=settings,
        )
    result = results[(target, "1300")]

    assert result.used_exhaustive_fallback is True
    assert not removed_donors & {(n.material, n.plant) for n in result.outcome.neighbours}
    assert len(result.outcome.neighbours) == POLICY.similarity.maximum_neighbours
    reference = _reference(world_, target, removed_donors)
    assert [(n.material, n.rank) for n in result.outcome.neighbours] == [
        (n.material, n.rank) for n in reference.neighbours
    ]
    assert result.outcome.estimate == reference.estimate


# --- Remove / restore, recalculation -----------------------------------------------------------


def test_removing_a_donor_reselects_neighbours_and_recalculates_with_the_unchanged_estimator(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target)
    before = _neighbours(factory, first.run_id, target)
    old = _recommendation(factory, recommendation_id)
    removed = before[0][0]
    others = _all_other_rows(factory, first.run_id, target)

    outcome = _exclude(world, recommendation_id, removed, reason="not comparable")

    reference = _reference(world, target, {(removed, "1300")})
    after = _neighbours(factory, first.run_id, target)
    assert [(m, p) for m, p, *_ in after] == [(n.material, n.plant) for n in reference.neighbours]
    assert removed not in [n[0] for n in after] and len(after) == len(before)
    # the stored target result is exactly what the unchanged estimator returns for those neighbours
    stored = _target_row(factory, first.run_id, target)
    assert stored[1] == reference.estimate.status.value == "SUCCESS"
    assert stored[2:5] == (
        reference.estimate.safety_stock, reference.estimate.rop, reference.estimate.max_stock
    )
    # the recommendation follows, with its workflow state and lineage untouched
    row = _recommendation(factory, recommendation_id)
    assert (row.recommended_safety_stock, row.recommended_rop, row.recommended_max_stock) == (
        Decimal(reference.estimate.safety_stock), Decimal(reference.estimate.rop),
        Decimal(reference.estimate.max_stock),
    )
    assert row.oar_neighbour_count == len(after)
    assert (row.status, row.chain_index, row.oar_run_id, row.feature_run_id) == (
        old.status, old.chain_index, old.oar_run_id, old.feature_run_id
    )
    assert row.current_version == old.current_version + 1
    with factory() as session:
        snapshot = session.execute(select(RecommendationVersion)).scalar_one()
    assert (snapshot.recommended_safety_stock, snapshot.recommended_rop, snapshot.recommended_max_stock) == (
        old.recommended_safety_stock, old.recommended_rop, old.recommended_max_stock
    )
    assert snapshot.changed_by == "tester" and removed in snapshot.reason
    # the exclusion is recorded, scoped to this target, with who/when/why and what was removed
    with factory() as session:
        saved = session.execute(select(OarDonorExclusion)).scalar_one()
        assert exclusions.load_active(session) == {(target, "1300"): frozenset({(removed, "1300")})}
    assert (saved.created_by, saved.reason, saved.donor_rank, saved.donor_similarity) == (
        "tester", "not comparable", before[0][2], before[0][3]
    )
    assert saved.created_at is not None and saved.restored_at is None and saved.is_active
    assert outcome.exclusion.id == saved.id
    assert _all_other_rows(factory, first.run_id, target) == others  # no other target moved


def test_the_same_donor_stays_eligible_for_a_different_target(world):
    factory = world[0]
    first = _run()
    with factory() as session:
        pairs = session.execute(
            select(OarNeighbour.neighbour_material, OarNeighbour.sap_material_number).where(
                OarNeighbour.oar_run_id == first.run_id
            )
        ).all()
    served_by: dict[str, set[str]] = {}
    for donor, served in pairs:
        served_by.setdefault(donor, set()).add(served)
    shared = sorted(d for d, targets in served_by.items() if len(targets) >= 2)
    assert shared, "the fixture must have a donor that two targets both use, or this proves nothing"
    donor = shared[0]
    excluded_for, still_served = sorted(served_by[donor])[:2]
    _exclude_directly(factory, excluded_for, donor)

    second = _run(force=True)

    with factory() as session:
        sharing = set(
            session.execute(
                select(OarNeighbour.sap_material_number).where(
                    OarNeighbour.oar_run_id == second.run_id, OarNeighbour.neighbour_material == donor
                )
            ).scalars()
        )
    assert excluded_for not in sharing  # gone from the target it was removed from
    assert still_served in sharing  # and still lent to the other one


def test_removing_twice_is_refused_and_creates_no_second_row(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target)
    removed = _neighbours(factory, first.run_id, target)[0][0]
    _exclude(world, recommendation_id, removed)

    with pytest.raises(oar_exclusions.OarExclusionError) as refused:
        _exclude(world, recommendation_id, removed)

    assert (refused.value.status_code, refused.value.code) == (409, "DONOR_ALREADY_EXCLUDED")
    with factory() as session:
        assert session.execute(select(func.count()).select_from(OarDonorExclusion)).scalar() == 1


def test_a_donor_that_is_not_a_neighbour_cannot_be_removed(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target)
    listed = {n[0] for n in _neighbours(factory, first.run_id, target)}
    stranger = next(f"D{i:04d}" for i in range(120) if f"D{i:04d}" not in listed)

    with pytest.raises(oar_exclusions.OarExclusionError) as refused:
        _exclude(world, recommendation_id, stranger)

    assert (refused.value.status_code, refused.value.code) == (404, "DONOR_NOT_A_NEIGHBOUR")


def test_restoring_keeps_the_audit_row_and_puts_the_donor_back_where_the_ranking_says(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target)
    original = _neighbours(factory, first.run_id, target)
    original_target = _target_row(factory, first.run_id, target)
    removed = original[0][0]
    _exclude(world, recommendation_id, removed)

    _restore(world, recommendation_id, removed, actor_id="restorer")

    assert _neighbours(factory, first.run_id, target) == original
    assert _target_row(factory, first.run_id, target) == original_target
    with factory() as session:
        row = session.execute(select(OarDonorExclusion)).scalar_one()  # still there: never deleted
        assert not row.is_active and row.active_slot == row.id
        assert row.restored_by == "restorer" and row.restored_at is not None and row.created_by == "tester"
        assert exclusions.load_active(session) == {}
    recommendation = _recommendation(factory, recommendation_id)
    assert recommendation.current_version == 3  # removed, then restored: each is a recorded change
    # and it can be removed again: a second history row, a new active one
    _exclude(world, recommendation_id, removed)
    with factory() as session:
        assert session.execute(select(func.count()).select_from(OarDonorExclusion)).scalar() == 2


def test_restoring_something_that_is_not_excluded_is_refused(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target)

    with pytest.raises(oar_exclusions.OarExclusionError) as refused:
        _restore(world, recommendation_id, _neighbours(factory, first.run_id, target)[0][0])

    assert (refused.value.status_code, refused.value.code) == (404, "EXCLUSION_NOT_FOUND")


# --- Minimum neighbours ------------------------------------------------------------------------


def test_a_removal_that_would_leave_too_few_donors_is_refused_and_saves_nothing(monkeypatch):
    world_ = build_world(monkeypatch, donors=2, targets=1, uniform=True)
    factory = world_[0]
    first = _run()
    target = "T0000"
    assert _target_row(factory, first.run_id, target)[0] == 2, "fixture must give exactly two donors"
    recommendation_id = _seed_recommendation(factory, first.run_id, target)
    neighbours_before = _neighbours(factory, first.run_id, target)
    target_before = _target_row(factory, first.run_id, target)
    recommendation_before = _recommendation(factory, recommendation_id)

    with pytest.raises(oar_exclusions.OarExclusionError) as refused:
        _exclude(world_, recommendation_id, neighbours_before[0][0])

    error = refused.value
    assert (error.status_code, error.code) == (409, "INSUFFICIENT_NEIGHBOURS_AFTER_EXCLUSION")
    assert error.details["minimum_neighbours"] == POLICY.similarity.minimum_neighbours == 2
    assert error.details["remaining_neighbours"] == 1
    with factory() as session:
        assert session.execute(select(func.count()).select_from(OarDonorExclusion)).scalar() == 0
        assert session.execute(select(func.count()).select_from(RecommendationVersion)).scalar() == 0
    assert _neighbours(factory, first.run_id, target) == neighbours_before
    assert _target_row(factory, first.run_id, target) == target_before
    after = _recommendation(factory, recommendation_id)
    assert (after.recommended_rop, after.current_version, after.status) == (
        recommendation_before.recommended_rop, recommendation_before.current_version,
        recommendation_before.status,
    )


def test_a_later_build_with_too_few_donors_left_reports_the_existing_not_evaluable_status(monkeypatch):
    world_ = build_world(monkeypatch, donors=2, targets=1, uniform=True)
    factory = world_[0]
    first = _run()
    _exclude_directly(factory, "T0000", _neighbours(factory, first.run_id, "T0000")[0][0])

    second = _run(force=True)

    row = _target_row(factory, second.run_id, "T0000")
    assert row[0] == 1  # the excluded donor is not used to make up the number
    assert row[1] == EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS.value
    assert row[2:5] == (None, None, None)  # nothing fabricated


# --- Approval protection -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["PENDING_APPROVAL", "APPROVED", "ADJUSTED", "HELD", "REJECTED", "SAP_EXECUTION_PENDING", "ADOPTED"],
)
def test_nothing_changes_once_the_recommendation_is_past_review(world, status):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target, status=status)
    before = (_neighbours(factory, first.run_id, target), _target_row(factory, first.run_id, target))

    with pytest.raises(oar_exclusions.OarExclusionError) as refused:
        _exclude(world, recommendation_id, before[0][0][0])

    assert (refused.value.status_code, refused.value.code) == (409, "RECOMMENDATION_NOT_EDITABLE")
    with factory() as session:
        assert session.execute(select(func.count()).select_from(OarDonorExclusion)).scalar() == 0
    assert (_neighbours(factory, first.run_id, target), _target_row(factory, first.run_id, target)) == before


def test_a_recommendation_sent_back_for_rework_may_be_changed_and_stays_sent_back(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target, status="SENT_BACK")

    _exclude(world, recommendation_id, _neighbours(factory, first.run_id, target)[0][0])

    assert _recommendation(factory, recommendation_id).status == "SENT_BACK"


# --- The generation run ------------------------------------------------------------------------


def test_generating_recommendations_honours_an_exclusion_saved_after_the_run(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    open_id = _seed_recommendation(factory, first.run_id, target)
    old = _recommendation(factory, open_id)
    removed = _neighbours(factory, first.run_id, target)[0][0]
    _exclude_directly(factory, target, removed)  # saved by something other than the API

    with factory() as session:
        oar_exclusions.apply_exclusions_before_generation(
            session, first.run_id, POLICY, provider=FakeProvider()
        )

    reference = _reference(world, target, {(removed, "1300")})
    assert removed not in [n[0] for n in _neighbours(factory, first.run_id, target)]
    row = _recommendation(factory, open_id)
    assert row.recommended_rop == Decimal(reference.estimate.rop)
    assert row.oar_neighbour_count == len(reference.neighbours)
    assert row.current_version == old.current_version + 1
    # the top donor is gone, so the best similarity the recommendation reports has moved with it
    assert row.oar_best_similarity != old.oar_best_similarity


def test_generation_leaves_a_submitted_recommendation_alone(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    approved_id = _seed_recommendation(factory, first.run_id, target, status="APPROVED")
    old = _recommendation(factory, approved_id)
    _exclude_directly(factory, target, _neighbours(factory, first.run_id, target)[0][0])

    with factory() as session:
        oar_exclusions.apply_exclusions_before_generation(
            session, first.run_id, POLICY, provider=FakeProvider()
        )

    row = _recommendation(factory, approved_id)
    assert (row.recommended_rop, row.recommended_safety_stock, row.current_version, row.status) == (
        old.recommended_rop, old.recommended_safety_stock, old.current_version, "APPROVED"
    )


def test_a_refresh_is_refused_when_the_run_is_no_longer_the_current_one(world):
    factory, _, _, _, settings = world
    first = _run()
    target = _pick_target(factory, first.run_id)
    with factory() as session:
        session.add(FeatureBuildRun(status="succeeded", policy_id="p", policy_version=1))
        session.commit()
        run = session.get(OarRun, first.run_id)
        with pytest.raises(refresh.RunNotCurrentError, match="newer feature run"):
            refresh.compute(
                session, run, {(target, "1300"): frozenset()}, policy=POLICY, provider=FakeProvider(),
                config=RetrievalConfig.from_settings(settings), settings=settings,
            )


# --- A host without the embedding model (the App Service) --------------------------------------


class NoModelHere:
    """What the App Service has: the library for fuzzy text, not sentence-transformers."""

    model_name = "BAAI/bge-base-en-v1.5"
    model_version = "BAAI/bge-base-en-v1.5+match-norm-1"
    is_available = False

    def encode(self, texts):
        return None


def _compute_for(world_, run_id, target, excluded, provider):
    factory, _, _, _, settings = world_
    with factory() as session:
        run = session.get(OarRun, run_id)
        return refresh.compute(
            session, run, {(target, "1300"): frozenset(excluded)}, policy=POLICY, provider=provider,
            config=RetrievalConfig.from_settings(settings), settings=settings,
        )[(target, "1300")]


def test_a_host_without_the_model_reselects_from_the_vectors_the_run_stored(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    removed = {(_neighbours(factory, first.run_id, target)[0][0], "1300")}

    with_model = _compute_for(world, first.run_id, target, removed, FakeProvider())
    without_model = _compute_for(world, first.run_id, target, removed, NoModelHere())

    assert [(n.material, n.rank, n.score.combined) for n in without_model.outcome.neighbours] == [
        (n.material, n.rank, n.score.combined) for n in with_model.outcome.neighbours
    ]
    assert without_model.outcome.estimate == with_model.outcome.estimate
    assert not removed & {(n.material, n.plant) for n in without_model.outcome.neighbours}


def test_without_the_model_the_stored_vectors_must_cover_every_description(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    with factory() as session:
        session.query(OarEmbedding).delete()  # nothing stored, and no model to make them
        session.commit()

    with pytest.raises(refresh.RunNotCurrentError, match="neither the embedding model nor a stored vector"):
        _compute_for(world, first.run_id, target, set(), NoModelHere())


def test_an_older_algorithm_run_is_refused_even_when_the_vectors_are_there(world):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    with factory() as session:
        session.get(OarRun, first.run_id).algorithm_version = "oar-similarity-old"
        session.commit()

    with pytest.raises(refresh.RunNotCurrentError, match="algorithm version differs"):
        _compute_for(world, first.run_id, target, set(), FakeProvider())


# --- The API -----------------------------------------------------------------------------------


@pytest.fixture
def client(world):
    factory = world[0]

    def _session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _session
    app.dependency_overrides[get_embedding_provider] = lambda: FakeProvider()
    # the API builds its policy the way a run does; the fixture's matrix is what the run used
    monkeypatch_policy = oar_exclusions._default_policy
    oar_exclusions._default_policy = lambda: POLICY
    try:
        yield TestClient(app)
    finally:
        oar_exclusions._default_policy = monkeypatch_policy
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_embedding_provider, None)


def test_the_api_takes_the_actor_from_the_request_identity_not_the_body(world, client):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target)
    donor = _neighbours(factory, first.run_id, target)[0][0]
    url = f"/api/v1/i7/recommendations/{recommendation_id}/similar-materials/exclusions"

    smuggled = client.post(url, json={"donor_material": donor, "donor_plant": "1300", "actor_id": "mallory"})
    assert smuggled.status_code == 422  # a user id in the body is not accepted at all

    response = client.post(
        url,
        json={"donor_material": donor, "donor_plant": "1300", "reason": "wrong part family"},
        headers={"X-Actor-Id": "alice"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "READY_FOR_REVIEW" and body["current_version"] == 2
    similar = body["similar_materials"]
    assert donor not in [n["material"] for n in similar["neighbours"]]
    assert [(e["material"], e["excluded_by"], e["reason"]) for e in similar["excluded"]] == [
        (donor, "alice", "wrong part family")
    ]
    assert similar["can_edit"] is True and similar["minimum_neighbours"] == 2

    again = client.post(url, json={"donor_material": donor, "donor_plant": "1300"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "DONOR_ALREADY_EXCLUDED"

    restored = client.post(
        f"{url}/restore", json={"donor_material": donor, "donor_plant": "1300"}, headers={"X-Actor-Id": "bob"}
    )
    assert restored.status_code == 200, restored.text
    assert donor in [n["material"] for n in restored.json()["similar_materials"]["neighbours"]]
    assert restored.json()["similar_materials"]["excluded"] == []
    with factory() as session:
        row = session.execute(select(OarDonorExclusion)).scalar_one()
        assert (row.created_by, row.restored_by) == ("alice", "bob")


def test_the_api_reports_409_and_can_edit_false_for_a_submitted_recommendation(world, client):
    factory = world[0]
    first = _run()
    target = _pick_target(factory, first.run_id)
    recommendation_id = _seed_recommendation(factory, first.run_id, target, status="PENDING_APPROVAL")
    donor = _neighbours(factory, first.run_id, target)[0][0]
    url = f"/api/v1/i7/recommendations/{recommendation_id}/similar-materials"

    listing = client.get(url).json()
    assert listing["can_edit"] is False and "PENDING_APPROVAL" in listing["edit_blocked_reason"]

    response = client.post(f"{url}/exclusions", json={"donor_material": donor, "donor_plant": "1300"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RECOMMENDATION_NOT_EDITABLE"
