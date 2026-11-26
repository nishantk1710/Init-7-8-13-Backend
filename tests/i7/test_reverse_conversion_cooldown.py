"""The conversion cooldown: leave a material alone for a while after it changes between Min-Max and OAR.

The pure rules need nothing; the observation table is exercised on in-memory SQLite.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from reverse_conversion_support import FakeSource, attr, population, sparse, month
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy, current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion import cooldown, snapshot
from app.initiatives.i7.reverse_conversion.cooldown import (
    MIN_MAX,
    OAR,
    Observation,
    cooldown_ends,
    in_cooldown,
    observe,
    planning_group,
)
from app.initiatives.i7.reverse_conversion.evaluate import evaluate
from app.initiatives.i7.reverse_conversion.metrics import resolve_anchor
from app.initiatives.i7.reverse_conversion.service import build_report
from app.initiatives.i7.reverse_conversion.snapshot import run_reverse_conversion
from app.initiatives.i7.reverse_conversion.types import MaterialPlantInputs, ReverseCategory as Cat, ReverseReason as Why
from app.models.base import Base
from app.models.i7_features import MaterialFeature
from app.models.i7_reverse import (
    PlanningGroupObservation,
    ReverseApprovalLedger,
    ReverseConversionRequest,
    ReverseRecommendation,
    ReverseRun,
)

POLICY = current_reverse_conversion_policy()
TODAY = date(2026, 11, 26)
MM, OARS = frozenset({"VM", "VB"}), frozenset({"ND", "PD"})


# --- The setting ------------------------------------------------------------------------------------


def test_the_cooldown_is_twelve_months_to_begin_with_and_comes_from_a_setting():
    assert Settings().i7_conversion_cooldown_months == 12
    assert POLICY.cooldown_months == 12
    assert POLICY.parameters()["cooldown_months"] == 12  # shown with every other threshold


def test_changing_the_setting_changes_the_policy(monkeypatch):
    monkeypatch.setenv("I7_CONVERSION_COOLDOWN_MONTHS", "6")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert current_reverse_conversion_policy().cooldown_months == 6
    finally:
        monkeypatch.delenv("I7_CONVERSION_COOLDOWN_MONTHS")
        get_settings.cache_clear()


def test_the_policy_refuses_a_negative_cooldown():
    with pytest.raises(ValueError):
        ReverseConversionPolicy(cooldown_months=-1)


# --- Which group a material is in -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mrp", "group"),
    [("VM", MIN_MAX), ("vb", MIN_MAX), (" VM ", MIN_MAX), ("ND", OAR), ("pd", OAR), ("", None), (None, None), ("V1", None), ("NP", None)],
)
def test_the_group_comes_from_the_mrp_type(mrp, group):
    assert planning_group(mrp, MM, OARS) == group


# --- Seeing a change --------------------------------------------------------------------------------


def test_a_material_seen_for_the_first_time_has_no_change_date():
    changes = observe({}, {("M1", "1300"): MIN_MAX}, TODAY)
    assert changes.new == {("M1", "1300"): Observation(MIN_MAX, TODAY, None)} and changes.changed == {}


def test_the_same_group_as_last_time_writes_nothing():
    before = {("M1", "1300"): Observation(MIN_MAX, date(2026, 1, 5), None)}
    changes = observe(before, {("M1", "1300"): MIN_MAX}, TODAY)
    assert changes.new == {} and changes.changed == {}


def test_a_different_group_is_dated_from_the_run_that_saw_it_in_both_directions():
    before = {("M1", "1300"): Observation(OAR, date(2026, 1, 5), None), ("M2", "1500"): Observation(MIN_MAX, date(2026, 1, 5), None)}
    changes = observe(before, {("M1", "1300"): MIN_MAX, ("M2", "1500"): OAR}, TODAY)
    assert changes.changed == {
        ("M1", "1300"): Observation(MIN_MAX, date(2026, 1, 5), TODAY),  # OAR -> Min-Max
        ("M2", "1500"): Observation(OAR, date(2026, 1, 5), TODAY),  # Min-Max -> OAR
    }


def test_a_material_that_drops_out_of_view_keeps_its_row_and_is_not_a_change():
    before = {("M1", "1300"): Observation(OAR, date(2026, 1, 5), None)}
    changes = observe(before, {}, TODAY)
    assert changes.new == {} and changes.changed == {}


# --- The window -------------------------------------------------------------------------------------


def test_the_cooldown_ends_the_same_month_a_year_on():
    assert cooldown_ends(date(2026, 3, 17), 12) == date(2027, 3, 1)
    assert cooldown_ends(date(2026, 3, 17), 6) == date(2026, 9, 1)


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [(date(2026, 11, 26), True), (date(2027, 2, 28), True), (date(2027, 3, 1), False), (date(2028, 1, 1), False)],
)
def test_a_change_in_march_2026_cools_down_until_march_2027(as_of, expected):
    assert in_cooldown(date(2026, 3, 17), as_of, 12) is expected


def test_there_is_no_cooldown_when_the_change_is_unknown_the_setting_is_zero_or_today_is_not_given():
    assert not in_cooldown(None, TODAY, 12)
    assert not in_cooldown(date(2026, 3, 17), TODAY, 0)
    assert not in_cooldown(date(2026, 3, 17), None, 12)


# --- The decision -----------------------------------------------------------------------------------


def decide(changed_on, as_of=TODAY, cooldown=12, criticality="NORMAL", plant="1300", **kw):
    policy = POLICY.model_copy(update={"cooldown_months": cooldown})
    base = build_report(FakeSource(*population()), policy, resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=date(2026, 10, 1)))
    row = {r.material: r for r in base.rows}["REC1"]
    inputs = MaterialPlantInputs(
        material="REC1", plant=plant, mrp_type="VM", deletion_flag=False, criticality=criticality, lead_time_days=30,
        metrics=row.metrics, group_changed_on=changed_on, as_of=as_of, **kw,
    )
    return evaluate(inputs, policy)


def test_a_material_that_just_moved_is_not_recommended_and_says_until_when():
    d = decide(date(2026, 6, 10))
    assert (d.category, d.reason, d.rule) == (Cat.NOT_ELIGIBLE, Why.RECENTLY_CHANGED, 14)
    assert d.confidence is None
    assert "Jun 2026" in d.detail and "Jun 2027" in d.detail
    assert "anchor" not in d.detail.lower()


def test_once_the_cooldown_is_over_it_is_judged_on_its_usage_again():
    d = decide(date(2025, 6, 10))
    assert d.reason is not Why.RECENTLY_CHANGED and d.category is Cat.RECOMMENDED_FOR_OAR


def test_a_shorter_setting_ends_it_sooner():
    assert decide(date(2026, 6, 10), cooldown=4).reason is not Why.RECENTLY_CHANGED  # ended on 1 Oct 2026
    assert decide(date(2026, 6, 10), cooldown=18).reason is Why.RECENTLY_CHANGED


def test_a_material_never_seen_to_change_has_no_cooldown():
    assert decide(None).reason is not Why.RECENTLY_CHANGED


def test_the_cooldown_outranks_the_usage_and_criticality_rules_but_scope_still_comes_first():
    assert decide(date(2026, 6, 10), criticality="CRITICAL").reason is Why.RECENTLY_CHANGED  # not "protected criticality"
    assert decide(date(2026, 6, 10), plant="9999").category is Cat.EXCLUDED  # outside scope is decided first


def test_a_report_applies_the_cooldown_to_the_materials_the_source_says_changed():
    source = FakeSource(*population(), changes={("REC1", "1300"): date(2026, 6, 10)})
    anchor = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=date(2026, 10, 1))
    report = build_report(source, POLICY, anchor, as_of=TODAY)
    rows = {r.material: r for r in report.rows}
    assert (rows["REC1"].category, rows["REC1"].reason) == (Cat.NOT_ELIGIBLE, Why.RECENTLY_CHANGED)
    assert rows["REC2"].reason is not Why.RECENTLY_CHANGED
    assert report.reconciliation.reconciled


def test_a_report_without_today_applies_no_cooldown_and_does_not_ask_the_source():
    source = FakeSource(*population(), changes={("REC1", "1300"): date(2026, 6, 10)})
    anchor = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=date(2026, 10, 1))
    report = build_report(source, POLICY, anchor)
    assert {r.material: r for r in report.rows}["REC1"].reason is not Why.RECENTLY_CHANGED
    assert source.calls["group_changes"] == 0


# --- The stage keeps the observations ---------------------------------------------------------------

TABLES = [
    ReverseRun.__table__, ReverseRecommendation.__table__, ReverseConversionRequest.__table__, ReverseApprovalLedger.__table__,
    PlanningGroupObservation.__table__, MaterialFeature.__table__,
]


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setattr(
        snapshot,
        "get_settings",
        lambda: SimpleNamespace(i7_reverse_anchor_month="2026-10", i7_reverse_anchor_source="configured", i7_oar_mrp_type_set=OARS),
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=TABLES)
    return sessionmaker(bind=engine)


def set_mrp(factory, material, plant, mrp):
    with factory() as session:
        row = session.scalars(
            select(MaterialFeature).where(MaterialFeature.sap_material_number == material, MaterialFeature.sap_plant_code == plant)
        ).first()
        if row is None:
            session.add(
                MaterialFeature(
                    sap_material_number=material, sap_plant_code=plant, mrp_type=mrp, history_status="SUFFICIENT",
                    data_sufficiency="SUFFICIENT", adi_status="OK", cv_squared_status="OK", oar_scope="OUT_OF_SCOPE", feature_run_id=1,
                )
            )
        else:
            row.mrp_type = mrp
        session.commit()


def stage(factory, today):
    return run_reverse_conversion(
        session_factory=factory, source_factory=lambda s: FakeSource(*population(), changes=cooldown.changed_days(s)), today=today
    )


def observation(factory, material, plant):
    with factory() as session:
        return session.scalars(
            select(PlanningGroupObservation).where(
                PlanningGroupObservation.sap_material_number == material, PlanningGroupObservation.sap_plant_code == plant
            )
        ).one()


def test_the_first_run_records_each_group_with_no_change_date(factory):
    set_mrp(factory, "REC1", "1300", "VM")
    set_mrp(factory, "OARMAT", "1300", "ND")
    stage(factory, date(2026, 11, 26))
    assert (observation(factory, "REC1", "1300").planning_group, observation(factory, "REC1", "1300").changed_on) == (MIN_MAX, None)
    assert observation(factory, "OARMAT", "1300").planning_group == OAR


def test_a_material_that_moves_from_oar_to_min_max_is_dated_and_then_left_alone(factory):
    set_mrp(factory, "REC1", "1300", "ND")
    stage(factory, date(2026, 5, 1))  # first sight: it is OAR
    set_mrp(factory, "REC1", "1300", "VM")  # converted to Min-Max in SAP
    stage(factory, date(2026, 6, 10))  # the run that notices
    assert observation(factory, "REC1", "1300").changed_on == date(2026, 6, 10)
    with factory() as session:
        row = {r.material: r for r in snapshot.load_latest_report(session).rows}["REC1"]
    assert (row.category, row.reason) == (Cat.NOT_ELIGIBLE, Why.RECENTLY_CHANGED)  # not suggested back to OAR


def test_the_change_date_does_not_move_on_later_runs(factory):
    set_mrp(factory, "REC1", "1300", "ND")
    stage(factory, date(2026, 5, 1))
    set_mrp(factory, "REC1", "1300", "VM")
    stage(factory, date(2026, 6, 10))
    stage(factory, date(2026, 9, 1))
    assert observation(factory, "REC1", "1300").changed_on == date(2026, 6, 10)


def test_a_year_later_the_material_is_judged_on_its_usage_again(factory):
    set_mrp(factory, "REC1", "1300", "ND")
    stage(factory, date(2026, 5, 1))
    set_mrp(factory, "REC1", "1300", "VM")
    stage(factory, date(2026, 6, 10))
    stage(factory, date(2027, 7, 1))
    with factory() as session:
        row = {r.material: r for r in snapshot.load_latest_report(session).rows}["REC1"]
    assert row.reason is not Why.RECENTLY_CHANGED


def test_a_change_is_noticed_even_when_the_rest_of_the_stage_is_skipped(factory, monkeypatch):
    set_mrp(factory, "REC1", "1300", "VM")
    stage(factory, date(2026, 5, 1))
    set_mrp(factory, "REC1", "1300", "ND")  # converted to OAR in SAP
    monkeypatch.setattr(
        snapshot,
        "get_settings",
        lambda: SimpleNamespace(i7_reverse_anchor_month=None, i7_reverse_anchor_source="configured", i7_oar_mrp_type_set=OARS),
    )
    assert stage(factory, date(2026, 6, 10)).status == "succeeded"  # skipped for want of a month, never failed
    assert (observation(factory, "REC1", "1300").planning_group, observation(factory, "REC1", "1300").changed_on) == (OAR, date(2026, 6, 10))


def test_other_mrp_types_are_not_watched(factory):
    set_mrp(factory, "OTHER", "1300", "V1")
    stage(factory, date(2026, 5, 1))
    with factory() as session:
        assert session.scalars(select(PlanningGroupObservation).where(PlanningGroupObservation.sap_material_number == "OTHER")).first() is None
