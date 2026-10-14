"""Adoption tracking says which field changed, when, and how many days after.

The SAP change-history reader is an interface, so everything here runs on fake
change rows -- no database, no SAP. What it cannot prove is the real extract's
date and time format: the delivered CDPOS has no MATERIAL change rows at all, so
the parsing is checked against the formats SAP and the normalise view are known
to produce, not against a real MARC change document.
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7.adoption import _approval_dates
from app.initiatives.i7.adapters.change_documents import ChangeDocumentRow
from app.initiatives.i7.recommendations import sap_change_documents
from app.initiatives.i7.recommendations.adoption import (
    _as_decimal,
    _is_populated,
    _same_number,
    evaluate_conversion_adoption,
    evaluate_parameter_adoption,
)
from app.initiatives.i7.recommendations.adoption_report import (
    build_field_reports,
    check_type_label,
    is_pending,
    plain_detail,
)
from app.initiatives.i7.recommendations.sap_change_documents import (
    RawChangeDocumentProvider,
    _parse_changed,
)
from app.initiatives.i7.recommendations.types import AdoptionFieldChange, AdoptionStatus
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation


def change(field, old, new, day="2026-10-14", time="103200", doc="0090012345", key="0000000080000000011500"):
    return ChangeDocumentRow(
        change_doc_object="MATERIAL",
        object_value="000000008000000001",
        document_number=doc,
        date=day,
        time=time,
        table_name="MARC",
        table_key=key,
        field_name=field,
        change_indicator="U",
        new_value=new,
        old_value=old,
    )


class FakeState:
    """A provider that serves fixed SAP state and field changes."""

    def __init__(self, state=None, changes=None):
        self._state, self._changes = state, changes

    def current_state(self, material, plant):
        return self._state

    def field_changes(self, material, plant):
        return self._changes


# --- numbers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("350", "350"), ("350.000", "350"), ("1,500.000", "1500"), ("5.000-", "-5"), (" 12 ", "12")],
)
def test_sap_quantity_text_reads_as_the_same_number(raw, expected):
    assert _same_number(raw, expected)


def test_different_numbers_and_non_numbers_are_not_the_same():
    assert not _same_number("351", "350")
    assert not _same_number(None, "350")
    assert _same_number("VB", "VB") and not _same_number("ND", "VB")
    assert _as_decimal("abc") is None and _as_decimal("") is None and _as_decimal(None) is None


def test_zero_in_any_format_is_not_populated():
    for zero in ("0", "0.00", "0.000", " 0 ", None, ""):
        assert not _is_populated(zero)
    assert _is_populated("435.000") and _is_populated("1")


def test_a_quantity_stored_with_decimals_counts_as_adopted():
    result = evaluate_parameter_adoption(
        "M1", "1500", 379, 435, 526,
        FakeState(state={"eisbe": "379.000", "minbe": "435.000", "mabst": "526.000"}),
    )
    assert result.status is AdoptionStatus.ADOPTED


def test_a_zero_with_decimals_is_not_a_filled_in_conversion_field():
    result = evaluate_conversion_adoption(
        "M1", "1500", "VB", FakeState(state={"mrp_type": "VB", "minbe": "0.000", "mabst": "526.000"})
    )
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED
    assert "minbe_populated" in result.mismatched_fields


# --- reading the change rows -------------------------------------------------


@pytest.mark.parametrize(
    "raw_date, raw_time, expected_date, expected_time",
    [
        ("2026-10-14", "103200", date(2026, 10, 14), "10:32"),
        ("20261014", "10:32:00", date(2026, 10, 14), "10:32"),
        ("14.10.2026", "0932", date(2026, 10, 14), "09:32"),
        (date(2026, 10, 14), None, date(2026, 10, 14), None),
        ("2026-10-14", "not a time", date(2026, 10, 14), None),
        ("garbage", "103200", None, "10:32"),
        (None, None, None, None),
    ],
)
def test_change_dates_and_times_are_read_or_left_empty_never_guessed(
    raw_date, raw_time, expected_date, expected_time
):
    assert _parse_changed(raw_date, raw_time) == (expected_date, expected_time)


def _provider(monkeypatch, rows):
    calls = []

    def fake(session, padded, fields, window_start=None, window_end=None):
        calls.append((padded, window_start, window_end))
        return rows

    monkeypatch.setattr(sap_change_documents, "material_marc_changes", fake)
    return RawChangeDocumentProvider(None, recommendation_date=date(2026, 10, 11), monitoring_window_days=15), calls


def test_field_changes_keep_before_now_and_when(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        [
            change("MINBE", "0", "400", day="2026-10-12", time="080000"),
            change("MINBE", "400", "435", day="2026-10-14", time="103200", doc="0090012399"),
            change("MABST", "0", "526", day="2026-10-16", time="091000"),
        ],
    )
    changes = provider.field_changes("8000001", "1500")
    assert changes["minbe"] == AdoptionFieldChange("minbe", "0", "435", date(2026, 10, 14), "10:32", 2)
    assert changes["mabst"].changed_on == date(2026, 10, 16) and changes["mabst"].changes == 1
    assert "eisbe" not in changes


def test_field_changes_are_per_plant_and_empty_when_nothing_changed(monkeypatch):
    provider, _ = _provider(monkeypatch, [change("MINBE", "0", "435", key="00000000800000011300")])
    assert provider.field_changes("8000001", "1500") is None


def test_the_change_rows_are_read_once_for_state_and_changes(monkeypatch):
    provider, calls = _provider(monkeypatch, [change("MINBE", "0", "435")])
    provider.current_state("8000001", "1500")
    provider.field_changes("8000001", "1500")
    assert len(calls) == 1
    # window: the anchor date to anchor + 15 days
    assert calls[0][1:] == ("2026-10-11", "2026-10-26")


# --- sentences -----------------------------------------------------------------


ANCHOR = date(2026, 10, 11)


def _parameter_result(state):
    return evaluate_parameter_adoption("M1", "1500", 379, 435, 526, FakeState(state=state))


def test_an_adopted_check_lists_each_field_with_dates_and_days():
    changes = {
        "minbe": AdoptionFieldChange("minbe", "0", "435", date(2026, 10, 14), "10:32", 1),
        "mabst": AdoptionFieldChange("mabst", "0", "526", date(2026, 10, 16), "09:10", 1),
        "eisbe": AdoptionFieldChange("eisbe", "0", "379", date(2026, 10, 14), None, 1),
    }
    result = _parameter_result({"eisbe": "379", "minbe": "435", "mabst": "526"})
    reports = build_field_reports(result, changes, ANCHOR)
    assert [r.label for r in reports] == ["Safety stock", "Reorder point", "Max stock"]
    assert [r.days_after for r in reports] == [3, 3, 5]
    text = plain_detail(result, reports, anchor_name="approval", window_days=15, window_open=True)
    assert "Reorder point: 0 → 435 on 14 Oct 2026, 10:32 (3 days after the approval)." in text
    assert "Max stock: 0 → 526 on 16 Oct 2026, 09:10 (5 days after the approval)." in text
    assert "Safety stock: 0 → 379 on 14 Oct 2026 (3 days after the approval)." in text
    assert check_type_label(reports, False) == "Safety stock · Reorder point · Max stock"


def test_a_partial_check_says_which_field_did_not_change():
    changes = {"minbe": AdoptionFieldChange("minbe", "0", "435", date(2026, 10, 14), "10:32", 1)}
    result = _parameter_result({"minbe": "435"})
    assert result.status is AdoptionStatus.PARTIALLY_ADOPTED
    reports = build_field_reports(result, changes, ANCHOR)
    text = plain_detail(result, reports, anchor_name="approval", window_days=15, window_open=True)
    assert "Reorder point: 0 → 435" in text
    assert "Safety stock: no change recorded in SAP." in text
    assert "Max stock: no change recorded in SAP." in text


def test_a_change_to_the_wrong_value_says_what_was_approved():
    changes = {"minbe": AdoptionFieldChange("minbe", "0", "300", date(2026, 10, 14), "10:32", 1)}
    result = evaluate_parameter_adoption("M1", "1500", None, 435, None, FakeState(state={"minbe": "300"}))
    text = plain_detail(
        result, build_field_reports(result, changes, ANCHOR), anchor_name="approval", window_days=15, window_open=True
    )
    assert "Reorder point: 0 → 300 on 14 Oct 2026, 10:32 (3 days after the approval), but 435 was approved." in text


def test_a_change_on_the_same_day_or_before_is_worded_correctly():
    changes = {"minbe": AdoptionFieldChange("minbe", "0", "435", ANCHOR, None, 1)}
    result = evaluate_parameter_adoption("M1", "1500", None, 435, None, FakeState(state={"minbe": "435"}))
    same = plain_detail(
        result, build_field_reports(result, changes, ANCHOR), anchor_name="approval", window_days=15, window_open=True
    )
    assert "(the same day as the approval)" in same
    one = build_field_reports(result, {"minbe": AdoptionFieldChange("minbe", "0", "435", date(2026, 10, 12), None, 1)}, ANCHOR)
    assert "(1 day after the approval)" in plain_detail(result, one, anchor_name="approval", window_days=15, window_open=True)


def test_no_evidence_is_pending_while_the_window_is_open_then_says_so():
    result = evaluate_parameter_adoption("M1", "1500", 379, 435, 526, FakeState(state=None))
    assert result.status is AdoptionStatus.UNKNOWN
    reports = build_field_reports(result, None, ANCHOR)
    assert all(r.matched is None and r.now is None for r in reports)
    assert plain_detail(result, reports, anchor_name="approval", window_days=15, window_open=True) == (
        "No change recorded in SAP yet."
    )
    assert plain_detail(result, reports, anchor_name="approval", window_days=15, window_open=False) == (
        "No change recorded in SAP within 15 days of the approval."
    )
    assert is_pending(result.status, True) and not is_pending(result.status, False)
    assert not is_pending(AdoptionStatus.ADOPTED, True)


def test_a_conversion_check_names_mrp_type_reorder_point_and_max_stock():
    result = evaluate_conversion_adoption(
        "M1", "1500", "VB", FakeState(state={"mrp_type": "VB", "minbe": "435", "mabst": "526"})
    )
    changes = {
        "dismm": AdoptionFieldChange("dismm", "ND", "VB", date(2026, 10, 14), "10:32", 1),
        "minbe": AdoptionFieldChange("minbe", "0", "435", date(2026, 10, 14), "10:32", 1),
        "mabst": AdoptionFieldChange("mabst", "0", "526", date(2026, 10, 14), "10:32", 1),
    }
    reports = build_field_reports(result, changes, ANCHOR)
    assert check_type_label(reports, True) == "MRP type · Reorder point · Max stock"
    assert [r.expected for r in reports] == ["VB", "filled in", "filled in"]
    assert "MRP type: ND → VB on 14 Oct 2026, 10:32" in plain_detail(
        result, reports, anchor_name="approval", window_days=15, window_open=True
    )


def test_with_no_fields_the_check_type_falls_back_to_a_plain_name():
    assert check_type_label((), True) == "Min-Max conversion"
    assert check_type_label((), False) == "Planning parameters"


# --- approval dates (SQLite) ------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # A copy without indexes: the model declares one index both on the column and
    # as an explicit Index, which SQL Server tolerates and SQLite does not.
    metadata = MetaData()
    for model in (Recommendation, ApprovalLedgerEntry):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def _ledger(action, new_status, stamp, rec_id="REC-STD-M1-1500", version=1):
    return ApprovalLedgerEntry(
        recommendation_id=rec_id,
        recommendation_version=version,
        actor_id="u1",
        actor_role="Plant Head",
        action=action,
        previous_status="PENDING_APPROVAL",
        new_status=new_status,
        timestamp=stamp,
    )


def test_the_approval_date_is_the_final_approval_not_an_earlier_role(session):
    session.add_all(
        [
            _ledger("APPROVE", "PENDING_APPROVAL", datetime(2026, 10, 9, 8, tzinfo=timezone.utc)),
            _ledger("APPROVE", "APPROVED", datetime(2026, 10, 11, 9, tzinfo=timezone.utc)),
            _ledger("HOLD", "HELD", datetime(2026, 10, 12, 9, tzinfo=timezone.utc)),
        ]
    )
    session.commit()
    assert _approval_dates(session, ["REC-STD-M1-1500", "REC-NONE"]) == {"REC-STD-M1-1500": date(2026, 10, 11)}
    assert _approval_dates(session, []) == {}
