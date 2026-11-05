"""SAP adoption: the window starts at the SAP handover, and SAP is checked every day.

SQLite with only the tables the check reads. SAP's change history is a fake that honours the window it is
asked for, so the tests prove *which* changes the window lets in, not just what happens to the ones it does.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i7 import adoption as adoption_api
from app.initiatives.i7.adapters.change_documents import ChangeDocumentRow
from app.initiatives.i7.pipeline_scheduler import SchedulePolicy, adoption_check_step, tick
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import sap_change_documents
from app.initiatives.i7.recommendations.types import AdoptionStatus
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, SapAdoptionResult

HANDED_OFF = date(2026, 10, 20)  # a long approval: generated on 1 Oct, handed to SAP on the 20th
WINDOW_DAYS = PolicyDocument().adoption.monitoring_window_days
WINDOW_END = date(2026, 10, 27)  # 20 Oct + 7 days


def utc(year, month, day, hour=9):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, SapAdoptionResult, ApprovalLedgerEntry):
        model.__table__.to_metadata(metadata).indexes.clear()  # declared twice; SQLite refuses that
    metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def sap(monkeypatch):
    """SAP's change history for material M1 at plant 1300, as a list the test edits. Honours the window."""
    rows: list[ChangeDocumentRow] = []
    calls: list[tuple] = []
    reach: dict = {"through": None}  # the newest date the change history holds

    def fake(session, padded_material, tracked_fields, window_start=None, window_end=None):
        calls.append((window_start, window_end))
        return [
            r for r in rows
            if (window_start is None or r.date >= window_start) and (window_end is None or r.date <= window_end)
        ]

    monkeypatch.setattr(sap_change_documents, "material_marc_changes", fake)
    monkeypatch.setattr(sap_change_documents, "material_history_through", lambda session: reach["through"])
    fake.rows, fake.calls, fake.reach = rows, calls, reach
    return fake


def change(field, new, day, old="0"):
    return ChangeDocumentRow(
        change_doc_object="MATERIAL", object_value="000000000000000M1", document_number=f"D{day}{field}",
        date=day, time="103200", table_name="MARC", table_key="M11300", field_name=field,
        change_indicator="U", new_value=new, old_value=old,
    )


def add_recommendation(session, material="M1", *, status="SAP_EXECUTION_PENDING", handed_off=HANDED_OFF, rop=10):
    """A stock item with a new reorder point to adopt. ``handed_off=None`` leaves no final approval on the ledger."""
    rid = f"REC-STD-{material}-1300"
    session.add(
        Recommendation(
            recommendation_id=rid, sap_material_number=material, sap_plant_code="1300",
            feature_run_id=1, forecast_run_id=1, inventory_run_id=1, oar_run_id=1, policy_id="p",
            policy_version=1, formula_version="f", status=status, impact_status="X", is_oar=False,
            recommended_rop=rop, current_rop=None, generated_at=utc(2026, 10, 1),
        )
    )
    session.add(ApprovalLedgerEntry(
        recommendation_id=rid, recommendation_version=1, actor_id="u", actor_role="Plant Head",
        action="APPROVE", previous_status="PENDING_APPROVAL", new_status="PENDING_APPROVAL",
        timestamp=utc(2026, 10, 9),  # an earlier role: not the handover
    ))
    if handed_off is not None:
        session.add(ApprovalLedgerEntry(
            recommendation_id=rid, recommendation_version=1, actor_id="u2", actor_role="Plant Head",
            action="APPROVE", previous_status="PENDING_APPROVAL", new_status="SAP_EXECUTION_PENDING",
            timestamp=datetime(handed_off.year, handed_off.month, handed_off.day, 9, tzinfo=timezone.utc),
        ))
    session.commit()
    return rid


def view(factory, rid, today, handed_off=HANDED_OFF):
    with factory() as session:
        row = session.execute(select(Recommendation).where(Recommendation.recommendation_id == rid)).scalar_one()
        return adoption_api._build_view(session, row, PolicyDocument(), handed_off, today)


def results(factory, rid):
    with factory() as session:
        return session.execute(select(SapAdoptionResult).where(SapAdoptionResult.recommendation_id == rid)).scalars().all()


# --- the window starts at the SAP handover ---------------------------------------------------------------------


def test_the_window_is_seven_days_and_is_set_in_one_place():
    assert PolicyDocument().adoption.monitoring_window_days == WINDOW_DAYS == 7


def test_the_window_runs_from_the_handover_not_from_when_the_recommendation_was_generated(factory, sap):
    rid = add_recommendation(factory())

    v = view(factory, rid, today=date(2026, 10, 25))

    assert v.approved_on == HANDED_OFF
    assert v.window_ends_on == WINDOW_END  # 20 Oct + 7 days; from generated_at (1 Oct) it would be 8 Oct
    assert sap.calls[-1] == ("2026-10-20", "2026-10-27")


def test_a_change_made_while_waiting_for_approval_does_not_count(factory, sap):
    """Made on 10 Oct, between generation and the handover: SAP was not asked to do it yet."""
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "10", "2026-10-10"))

    v = view(factory, rid, today=date(2026, 10, 25))

    assert v.result.status is AdoptionStatus.UNKNOWN
    assert v.pending


def test_the_handover_day_itself_counts(factory, sap):
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "10", "2026-10-20"))

    assert view(factory, rid, today=date(2026, 10, 25)).result.status is AdoptionStatus.ADOPTED


def test_a_recommendation_not_yet_handed_over_has_no_window_and_never_reads_sap(factory, sap):
    rid = add_recommendation(factory(), status="PENDING_APPROVAL", handed_off=None)
    sap.rows.append(change("MINBE", "10", "2026-10-10"))  # would match, if anyone looked

    v = view(factory, rid, today=date(2026, 12, 31), handed_off=None)

    assert sap.calls == []
    assert v.result.status is AdoptionStatus.UNKNOWN
    assert v.pending and v.window_open
    assert v.window_ends_on is None and v.approved_on is None
    assert "Waiting for final approval" in v.detail


def test_a_long_approval_never_runs_the_window_out(factory, sap):
    """Still waiting two months after generation: pending, not expired."""
    rid = add_recommendation(factory(), status="PENDING_APPROVAL", handed_off=None)

    v = view(factory, rid, today=date(2026, 12, 1), handed_off=None)

    assert v.pending and v.window_open


# --- what the check decides ------------------------------------------------------------------------------------


def test_a_matching_change_is_adopted_as_soon_as_it_is_there_with_the_window_still_open(factory, sap):
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "10.000", "2026-10-22"))

    v = view(factory, rid, today=date(2026, 10, 23))

    assert v.window_open
    assert v.result.status is AdoptionStatus.ADOPTED
    assert not v.pending


def test_while_the_window_is_open_a_change_that_does_not_match_stays_pending(factory, sap):
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "7", "2026-10-22"))

    v = view(factory, rid, today=date(2026, 10, 23))

    assert v.result.status is AdoptionStatus.UNKNOWN and v.pending
    assert "MINBE" in " ".join(f.label.upper() for f in v.reports) or "Reorder point: 0 → 7" in v.detail


def test_the_last_day_of_the_window_is_still_open(factory, sap):
    rid = add_recommendation(factory())
    sap.reach["through"] = "2026-11-30"

    assert view(factory, rid, today=WINDOW_END).window_open
    assert not view(factory, rid, today=date(2026, 10, 28)).window_open


def test_once_the_window_has_closed_with_complete_history_a_non_matching_change_is_not_adopted(factory, sap):
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "7", "2026-10-22"))
    sap.reach["through"] = "2026-10-28"

    v = view(factory, rid, today=date(2026, 10, 28))

    assert not v.window_open
    assert v.result.status is AdoptionStatus.NOT_ADOPTED
    assert not v.pending


def test_with_complete_history_and_no_change_at_all_the_closed_window_is_not_adopted(factory, sap):
    rid = add_recommendation(factory())
    sap.reach["through"] = "2026-10-27"  # exactly the window's last day: complete

    assert view(factory, rid, today=date(2026, 11, 3)).result.status is AdoptionStatus.NOT_ADOPTED


def test_history_that_stops_before_the_window_ends_keeps_it_unknown(factory, sap):
    rid = add_recommendation(factory())
    sap.reach["through"] = "2026-10-26"  # one day short

    v = view(factory, rid, today=date(2026, 11, 3))

    assert v.result.status is AdoptionStatus.UNKNOWN
    assert "not complete through 2026-10-27" in v.detail


def test_incomplete_history_also_keeps_a_non_matching_change_unknown(factory, sap):
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "7", "2026-10-22"))
    sap.reach["through"] = "2026-10-24"

    assert view(factory, rid, today=date(2026, 11, 3)).result.status is AdoptionStatus.UNKNOWN


def test_a_matching_change_is_adopted_even_when_the_history_is_otherwise_incomplete(factory, sap):
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "10", "2026-10-22"))
    sap.reach["through"] = None

    assert view(factory, rid, today=date(2026, 11, 3)).result.status is AdoptionStatus.ADOPTED


def test_a_change_after_the_window_is_not_counted(factory, sap):
    rid = add_recommendation(factory())
    sap.rows.append(change("MINBE", "10", "2026-10-28"))
    sap.reach["through"] = "2026-11-30"

    assert view(factory, rid, today=date(2026, 11, 6)).result.status is AdoptionStatus.NOT_ADOPTED


# --- missing change history ------------------------------------------------------------------------------------


def test_no_change_history_at_all_is_unknown_and_pending_while_open(factory, sap):
    rid = add_recommendation(factory())

    v = view(factory, rid, today=date(2026, 10, 25))

    assert v.result.status is AdoptionStatus.UNKNOWN
    assert v.pending
    assert "No change recorded in SAP yet" in v.detail


def test_no_change_history_stays_unknown_after_the_window_closes_never_not_adopted(factory, sap):
    rid = add_recommendation(factory())  # reach["through"] is None: SAP has no material history at all

    v = view(factory, rid, today=date(2026, 12, 1))

    assert v.result.status is AdoptionStatus.UNKNOWN
    assert not v.pending  # the window is over, but nothing was recorded: still not a "not adopted"


def test_change_rows_for_other_fields_or_plants_are_ignored(factory, sap):
    rid = add_recommendation(factory())
    other_plant = change("MINBE", "10", "2026-10-22")._replace(table_key="M11500")
    sap.rows.append(other_plant)

    assert view(factory, rid, today=date(2026, 10, 25)).result.status is AdoptionStatus.UNKNOWN


# --- the daily run ---------------------------------------------------------------------------------------------


def test_the_daily_run_adopts_a_recommendation_the_day_its_change_appears(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)

    day_one = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 22))
    assert (day_one.checked, day_one.pending, day_one.adopted) == (1, 1, 0)
    assert [r.status for r in results(factory, rid)] == ["UNKNOWN"]

    sap.rows.append(change("MINBE", "10", "2026-10-23"))
    day_two = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 23))

    assert (day_two.checked, day_two.adopted) == (1, 1)
    [row] = results(factory, rid)  # updated in place: still one row
    assert row.status == "ADOPTED"


def test_running_twice_in_a_day_changes_nothing(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)
    sap.rows.append(change("MINBE", "10", "2026-10-23"))

    first = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 23))
    calls_after_first = len(sap.calls)
    second = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 23))

    assert (first.checked, second.checked, second.skipped) == (1, 0, 1)
    assert len(sap.calls) == calls_after_first  # SAP history not read again
    assert len(results(factory, rid)) == 1


def test_an_adopted_recommendation_is_not_checked_again_on_later_days(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)
    sap.rows.append(change("MINBE", "10", "2026-10-23"))
    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 23))

    later = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 24))

    assert (later.checked, later.skipped) == (0, 1)
    assert len(results(factory, rid)) == 1


def test_a_pending_recommendation_is_checked_again_each_day(factory, sap):
    with factory() as session:
        add_recommendation(session)

    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 22))
    next_day = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 23))

    assert next_day.checked == 1


def test_a_partial_match_can_become_a_full_one_later_in_the_window(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)
        row = session.execute(select(Recommendation)).scalar_one()
        row.recommended_max_stock = 50
        session.commit()
    sap.rows.append(change("MINBE", "10", "2026-10-22"))
    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 22))
    assert [r.status for r in results(factory, rid)] == ["PARTIALLY_ADOPTED"]

    sap.rows.append(change("MABST", "50", "2026-10-24"))
    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 24))

    assert [r.status for r in results(factory, rid)] == ["ADOPTED"]


def test_after_the_window_closes_one_last_verdict_is_recorded_and_then_the_run_stops_checking(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)
    sap.rows.append(change("MINBE", "7", "2026-10-22"))  # never matches
    sap.reach["through"] = "2026-11-10"

    adoption_api.run_daily_adoption_check(factory, WINDOW_END)  # last open day: Pending
    assert [r.status for r in results(factory, rid)] == ["UNKNOWN"]
    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 28))  # closed: the verdict
    assert [r.status for r in results(factory, rid)] == ["NOT_ADOPTED"]
    later = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 29))

    assert (later.checked, later.skipped) == (0, 1)


def test_a_closed_window_left_unknown_by_incomplete_history_is_decided_when_the_extract_catches_up(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)
    sap.reach["through"] = "2026-10-24"

    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 28))
    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 29))
    assert [r.status for r in results(factory, rid)] == ["UNKNOWN"]  # checked again, still undecidable

    sap.reach["through"] = "2026-10-29"  # a newer extract now covers the window
    caught_up = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 30))

    assert (caught_up.checked, caught_up.not_adopted) == (1, 1)
    assert [r.status for r in results(factory, rid)] == ["NOT_ADOPTED"]


def test_a_late_arriving_matching_change_still_wins_after_an_undecided_close(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)
    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 28))  # no history: Unknown

    sap.rows.append(change("MINBE", "10", "2026-10-26"))  # the extract arrives with a change made inside the window
    adoption_api.run_daily_adoption_check(factory, date(2026, 10, 29))

    assert [r.status for r in results(factory, rid)] == ["ADOPTED"]


def test_the_daily_run_leaves_recommendations_still_in_approval_alone(factory, sap):
    with factory() as session:
        rid = add_recommendation(session, status="PENDING_APPROVAL", handed_off=None)

    summary = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 25))

    assert summary.checked == 0
    assert results(factory, rid) == []
    assert sap.calls == []


def test_a_handed_over_recommendation_with_no_approval_on_the_ledger_is_skipped_not_guessed(factory, sap):
    with factory() as session:
        rid = add_recommendation(session, handed_off=None)  # status says handed over; the ledger does not

    summary = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 25))

    assert (summary.checked, summary.skipped) == (0, 1)
    assert results(factory, rid) == []


def test_missing_change_history_keeps_every_recommendation_unknown_across_runs(factory, sap):
    with factory() as session:
        for material in ("M1", "M2"):
            add_recommendation(session, material)

    first = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 22))
    second = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 23))

    assert (first.pending, second.pending, first.adopted + second.adopted) == (2, 2, 0)
    with factory() as session:
        assert {r.status for r in session.execute(select(SapAdoptionResult)).scalars()} == {"UNKNOWN"}
        assert len(session.execute(select(SapAdoptionResult)).scalars().all()) == 2


def test_a_row_duplicated_by_an_old_race_does_not_stop_the_check(factory, sap):
    with factory() as session:
        rid = add_recommendation(session)
        for _ in range(2):
            session.add(SapAdoptionResult(recommendation_id=rid, status="UNKNOWN", evaluated_at=utc(2026, 10, 21)))
        session.commit()
    sap.rows.append(change("MINBE", "10", "2026-10-23"))

    summary = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 23))

    assert (summary.checked, summary.failed, summary.adopted) == (1, 0, 1)


def test_one_failing_recommendation_does_not_stop_the_rest(factory, sap, monkeypatch):
    with factory() as session:
        add_recommendation(session, "M1")
        add_recommendation(session, "M2")
    real = adoption_api._build_view

    def flaky(session, row, policy, handed_off_on, today=None):
        if row.sap_material_number == "M1":
            raise RuntimeError("boom")
        return real(session, row, policy, handed_off_on, today)

    monkeypatch.setattr(adoption_api, "_build_view", flaky)

    summary = adoption_api.run_daily_adoption_check(factory, date(2026, 10, 22))

    assert (summary.checked, summary.failed) == (1, 1)


# --- the nightly timer -----------------------------------------------------------------------------------------


def policy(**kw):
    return SchedulePolicy(hour_utc=20, retries=0, interval_seconds=300, initial_delay_seconds=0, **kw)


def at(hour, minute, day=1):
    return datetime(2026, 10, 20 + day, hour, minute, tzinfo=timezone.utc)


def pipeline_quiet(monkeypatch):
    from app.initiatives.i7 import pipeline_scheduler

    monkeypatch.setattr(pipeline_scheduler, "_tick_pipeline", lambda *a, **k: "no run: unchanged")


def test_the_nightly_tick_runs_the_adoption_step_when_it_is_on(monkeypatch):
    pipeline_quiet(monkeypatch)
    seen = []

    out = tick(
        policy(adoption_check_enabled=True), session_factory=lambda: None, runner=lambda r: "",
        now=at(20, 5), adoption=lambda factory, now: seen.append(now) or "2 checked",
    )

    assert seen == [at(20, 5)]
    assert "adoption: 2 checked" in out


def test_the_nightly_tick_skips_the_adoption_step_when_it_is_off(monkeypatch):
    pipeline_quiet(monkeypatch)
    seen = []

    tick(
        policy(adoption_check_enabled=False), session_factory=lambda: None, runner=lambda r: "",
        now=at(20, 5), adoption=lambda factory, now: seen.append(now) or "x",
    )

    assert seen == []


def test_a_failing_adoption_step_is_reported_and_never_raised(monkeypatch):
    pipeline_quiet(monkeypatch)

    def boom(factory, now):
        raise RuntimeError("db down")

    out = tick(
        policy(adoption_check_enabled=True), session_factory=lambda: None, runner=lambda r: "",
        now=at(20, 5), adoption=boom,
    )

    assert "adoption failed: RuntimeError" in out


def test_the_step_does_nothing_more_once_it_has_finished_cleanly_for_the_day(factory, sap):
    with factory() as session:
        add_recommendation(session)
    step = adoption_check_step()

    first = step(factory, datetime(2026, 10, 22, 20, 5, tzinfo=timezone.utc))
    again = step(factory, datetime(2026, 10, 22, 20, 10, tzinfo=timezone.utc))
    next_day = step(factory, datetime(2026, 10, 23, 20, 5, tzinfo=timezone.utc))

    assert first.startswith("1 checked")
    assert again == "already checked today"
    assert next_day.startswith("1 checked")
