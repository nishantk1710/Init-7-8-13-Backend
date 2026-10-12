"""Which upload is current, how months combine, and what an upload writes."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.integrations.criticality import reports
from app.integrations.criticality.report_parser import CriticalityReportError, parse_report
from app.models.criticality_report import CriticalityCurrent, CriticalityReportRow, CriticalityReportUpload
from app.models.ingestion import IngestionRun
from tests.criticality.conftest import workbook

JUL, AUG, SEP = date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)
TODAY = date(2026, 10, 12)


def _store(session, rows, month, *, plant="1300", replace=False, who="alice", name=None):
    parsed = parse_report(workbook(rows, plant=plant), plant=plant)
    return reports.store_upload(
        session,
        parsed,
        report_month=month,
        file_name=name or f"{plant}-{month:%b}.xlsx",
        uploaded_by=who,
        replace=replace,
        today=TODAY,
    )


def _current(session, plant="1300") -> dict[str, tuple]:
    rows = session.scalars(select(CriticalityCurrent).where(CriticalityCurrent.plant == plant))
    return {r.material: (r.criticality, r.carried_forward, r.tier_report_month) for r in rows}


# --- what an upload writes ------------------------------------------------


def test_an_upload_stores_the_file_the_rows_and_the_current_values(session) -> None:
    stored = _store(session, [("0001", "CRITICAL"), ("2", "NORMAL"), ("3", None)], JUL)
    upload = stored.upload
    assert (upload.plant, upload.report_month, upload.uploaded_by, upload.source) == ("1300", JUL, "alice", "upload")
    assert (upload.row_count, upload.tier_count, upload.blank_tiers) == (3, 2, 1)
    assert stored.is_current and stored.current_month == JUL and stored.replaced is None
    assert session.scalar(select(CriticalityReportRow.id).where(CriticalityReportRow.upload_id == upload.id).limit(1))
    assert _current(session) == {"1": ("CRITICAL", False, JUL), "2": ("NORMAL", False, JUL), "3": (None, False, None)}


def test_an_upload_leaves_an_ingestion_run_for_the_snapshot_fingerprints(session) -> None:
    _store(session, [("1", "NORMAL")], JUL, name="jul.xlsx")
    run = session.scalars(select(IngestionRun)).one()
    assert (run.target_table, run.status, run.row_count) == ("criticality_current", "succeeded", 1)
    assert run.source_file == "criticality:jul.xlsx"  # never read as an odata: delta


def test_a_plant_outside_scope_is_refused(session) -> None:
    parsed = parse_report(workbook([("1", "NORMAL", "4000")]), plant="4000")
    with pytest.raises(CriticalityReportError, match="not one of the platform's plants"):
        reports.store_upload(session, parsed, report_month=JUL, file_name="x.xlsx", uploaded_by="a", today=TODAY)


def test_a_future_month_is_refused(session) -> None:
    with pytest.raises(CriticalityReportError, match="has not started yet"):
        _store(session, [("1", "NORMAL")], date(2026, 11, 1))


def test_this_month_is_accepted(session) -> None:
    assert _store(session, [("1", "NORMAL")], date(2026, 10, 1)).is_current


def test_a_refused_upload_leaves_nothing_behind(session) -> None:
    _store(session, [("1", "NORMAL")], AUG)
    with pytest.raises(reports.DuplicateMonthError):
        _store(session, [("2", "NORMAL")], AUG)
    assert len(session.scalars(select(CriticalityReportUpload)).all()) == 1
    assert set(_current(session)) == {"1"}


# --- same plant, same month -----------------------------------------------


def test_the_same_month_needs_replace_and_the_duplicate_says_who(session) -> None:
    _store(session, [("1", "NORMAL")], AUG, who="alice", name="first.xlsx")
    with pytest.raises(reports.DuplicateMonthError, match=r"first\.xlsx, uploaded by alice"):
        _store(session, [("1", "IMPACT")], AUG)


def test_replace_makes_the_newer_file_current_and_keeps_both_in_history(session) -> None:
    first = _store(session, [("1", "NORMAL"), ("2", "NORMAL")], AUG, name="first.xlsx")
    second = _store(session, [("1", "IMPACT")], AUG, replace=True, who="bob", name="second.xlsx")
    assert second.replaced.id == first.upload.id
    history = {e.upload.file_name: e.status for e in reports.list_uploads(session)}
    assert history == {"second.xlsx": "current", "first.xlsx": "replaced"}
    # The replaced file contributes nothing: material 2 is gone, not carried.
    assert _current(session) == {"1": ("IMPACT", False, AUG)}


def test_the_other_plant_is_unaffected_by_a_replace(session) -> None:
    _store(session, [("1", "NORMAL")], AUG, plant="1500")
    _store(session, [("1", "CRITICAL")], AUG, plant="1300")
    _store(session, [("1", "IMPACT")], AUG, plant="1300", replace=True)
    assert _current(session, "1500") == {"1": ("NORMAL", False, AUG)}


# --- how months combine ---------------------------------------------------


def test_a_material_missing_from_a_newer_month_keeps_its_last_tier(session) -> None:
    _store(session, [("1", "CRITICAL"), ("2", "NORMAL")], JUL)
    _store(session, [("2", "IMPACT")], AUG)
    assert _current(session) == {"1": ("CRITICAL", True, JUL), "2": ("IMPACT", False, AUG)}


def test_a_blank_tier_in_a_newer_month_does_not_erase_the_older_one(session) -> None:
    _store(session, [("1", "CRITICAL")], JUL)
    _store(session, [("1", None)], AUG)
    assert _current(session) == {"1": ("CRITICAL", True, JUL)}


def test_carry_forward_runs_across_three_months(session) -> None:
    _store(session, [("1", "CRITICAL"), ("2", "NORMAL"), ("3", "OBSOLETE")], JUL)
    _store(session, [("2", "IMPACT")], AUG)
    _store(session, [("3", None)], SEP)
    assert _current(session) == {
        "1": ("CRITICAL", True, JUL),
        "2": ("IMPACT", True, AUG),
        "3": ("OBSOLETE", True, JUL),
    }


def test_the_newest_month_wins_a_disagreement(session) -> None:
    _store(session, [("1", "CRITICAL")], JUL)
    _store(session, [("1", "OBSOLETE")], AUG)
    assert _current(session)["1"][0] == "OBSOLETE"


def test_a_back_dated_upload_only_fills_what_newer_reports_leave_out(session) -> None:
    _store(session, [("1", "IMPACT")], AUG)
    stored = _store(session, [("1", "NORMAL"), ("2", "CRITICAL")], JUL)
    assert stored.is_current is False and stored.current_month == AUG
    assert _current(session) == {"1": ("IMPACT", False, AUG), "2": ("CRITICAL", True, JUL)}
    statuses = {e.upload.report_month: e.status for e in reports.list_uploads(session)}
    assert statuses == {AUG: "current", JUL: "previous"}


def test_description_and_types_follow_the_newest_report_that_states_them(session) -> None:
    _store(session, [("1", "NORMAL")], JUL)
    _store(session, [("1", "NORMAL")], AUG)
    row = session.scalars(select(CriticalityCurrent)).one()
    assert (row.description, row.mattyp, row.mrp_type) == ("desc 1", "ZREP", "PD")
    assert row.listed_report_month == AUG


def test_rebuilding_again_changes_nothing(session) -> None:
    _store(session, [("1", "CRITICAL")], JUL)
    _store(session, [("2", "NORMAL")], AUG)
    before = _current(session)
    reports.rebuild_current(session, "1300")
    session.commit()
    assert _current(session) == before


# --- history --------------------------------------------------------------


def _upload(id_, plant, month, source="upload", minute=0, hour=9):
    return CriticalityReportUpload(
        id=id_, plant=plant, report_month=month, file_name=f"{id_}.xlsx", sheet_name="S", source=source,
        dedupe_key=f"k{id_}", uploaded_by="x", uploaded_at=datetime(2026, 10, 1, hour, minute, tzinfo=timezone.utc),
        row_count=1,
    )


def test_each_plant_has_one_current_report_and_the_rest_are_previous_or_replaced() -> None:
    uploads = [
        _upload(1, "1300", JUL, source="july_delivery"),
        _upload(2, "1300", AUG, minute=1),
        _upload(3, "1300", AUG, minute=5),  # re-upload of August supersedes 2
        _upload(4, "1500", JUL, source="july_delivery"),
    ]
    status = {e.upload.id: e.status for e in reports.classify(uploads)}
    assert status == {3: "current", 2: "replaced", 1: "previous", 4: "current"}


def test_an_upload_beats_the_july_delivery_for_the_same_month() -> None:
    delivery = _upload(9, "1300", JUL, source="july_delivery", hour=23)  # imported LATER
    mine = _upload(1, "1300", JUL, hour=1)
    status = {e.upload.id: e.status for e in reports.classify([delivery, mine])}
    assert status == {1: "current", 9: "replaced"}


def test_history_is_newest_month_first_and_can_be_narrowed_to_a_plant(session) -> None:
    _store(session, [("1", "NORMAL")], JUL, plant="1300")
    _store(session, [("1", "NORMAL")], AUG, plant="1500")
    _store(session, [("1", "NORMAL")], AUG, plant="1300")
    assert reports.list_uploads(session)[0].upload.report_month == AUG
    assert {e.upload.plant for e in reports.list_uploads(session, "1500")} == {"1500"}


def test_current_by_plant_and_material_counts(session) -> None:
    _store(session, [("1", "NORMAL"), ("2", "NORMAL")], AUG, plant="1300")
    assert {p: u.report_month for p, u in reports.current_by_plant(session).items()} == {"1300": AUG}
    assert reports.materials_by_plant(session) == {"1300": 2}
