"""What the initiatives read: the shared port, I07's staging query, the July
baseline import, and the fingerprints an upload moves."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from app.core.criticality import CriticalityTier
from app.integrations.criticality import baseline, reports
from app.integrations.criticality.report_parser import parse_report
from app.integrations.criticality.zmm065 import NOT_LOADED, Zmm065CriticalitySource
from app.models.criticality_report import CriticalityCurrent
from tests.criticality.conftest import workbook

JUL, AUG = date(2026, 7, 1), date(2026, 8, 1)
TODAY = date(2026, 10, 12)


def _store(session, rows, month, plant="1300", **kw):
    return reports.store_upload(
        session, parse_report(workbook(rows, plant=plant), plant=plant),
        report_month=month, file_name=f"{plant}-{month:%b}.xlsx", uploaded_by="a", today=TODAY, **kw,
    )


@pytest.fixture
def source(factory, monkeypatch):
    monkeypatch.setattr("app.integrations.criticality.zmm065.get_sessionmaker", lambda: factory)
    return Zmm065CriticalitySource()


# --- the shared port ------------------------------------------------------


def test_nothing_loaded_reads_as_unknown_not_as_an_error(source) -> None:
    result = source.get("1", "1300")
    assert result.tier is None and result.reason == NOT_LOADED
    assert source.get_many([("1", "1300")])[("1", "1300")].reason == NOT_LOADED
    from app.core.criticality import CriticalityNotLoadedError

    with pytest.raises(CriticalityNotLoadedError):
        source.check_connection()


def test_a_missing_table_reads_as_not_loaded_too(monkeypatch) -> None:
    """Before the migration has run."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    bare = sessionmaker(bind=create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}))
    monkeypatch.setattr("app.integrations.criticality.zmm065.get_sessionmaker", lambda: bare)
    assert Zmm065CriticalitySource().get("1", "1300").reason == NOT_LOADED


def test_an_upload_changes_the_answer_the_port_gives(session, source) -> None:
    _store(session, [("8000004187", "NORMAL")], JUL)
    assert source.get("8000004187", "1300").tier is CriticalityTier.NORMAL
    _store(session, [("8000004187", "CRITICAL")], AUG)
    assert source.get("8000004187", "1300").tier is CriticalityTier.CRITICAL


def test_a_padded_number_finds_the_same_row(session, source) -> None:
    _store(session, [("000000008000004187", "IMPACT")], JUL)
    for asked in ("8000004187", "000000008000004187", " 8000004187 "):
        assert source.get(asked, "1300").tier is CriticalityTier.IMPACT, asked
    assert source.get_many([("000000008000004187", "1300")])[("000000008000004187", "1300")].tier is CriticalityTier.IMPACT


def test_criticality_is_per_plant_and_a_plantless_ask_is_withheld_when_ambiguous(session, source) -> None:
    _store(session, [("1", "CRITICAL"), ("2", "NORMAL")], JUL, plant="1300")
    _store(session, [("1", "NORMAL"), ("2", "NORMAL")], JUL, plant="1500")
    assert source.get("1", "1300").tier is CriticalityTier.CRITICAL
    assert source.get("1", "1500").tier is CriticalityTier.NORMAL
    ambiguous = source.get("1", None)
    assert ambiguous.tier is None and "differing tiers across plants" in ambiguous.reason
    assert source.get("2", None).tier is CriticalityTier.NORMAL  # same everywhere: unambiguous


def test_a_carried_forward_tier_is_what_the_port_returns(session, source) -> None:
    _store(session, [("1", "CRITICAL")], JUL)
    _store(session, [("2", "NORMAL")], AUG)
    assert source.get("1", "1300").tier is CriticalityTier.CRITICAL


def test_a_blank_material_has_no_tier_with_a_reason(session, source) -> None:
    _store(session, [("1", None)], JUL)
    result = source.get("1", "1300")
    assert result.tier is None and "not present at plant 1300" in result.reason


def test_get_many_answers_exactly_as_get_does(session, source) -> None:
    _store(session, [("1", "CRITICAL"), ("2", "NORMAL"), ("3", None)], JUL, plant="1300")
    _store(session, [("1", "IMPACT"), ("4", "OBSOLETE")], JUL, plant="1500")
    keys = [("1", "1300"), ("1", "1500"), ("1", None), ("2", None), ("3", "1300"), ("9", "1300"), ("9", None), ("4", "1300")]
    batch = source.get_many(keys)
    for key in keys:
        assert batch[key] == source.get(*key), key


# --- I07 staging ----------------------------------------------------------


@pytest.fixture
def i07_session(session, source, monkeypatch):
    """I07 staging asks the shared port, so point the port at the test database."""
    monkeypatch.setattr("app.shared.get_criticality_source", lambda: source)
    return session


def _i07_tiers(plants=("1300", "1500"), materials=("1", "2", "3", "4")) -> dict[tuple[str, str], str | None]:
    from app.initiatives.i7.adapters.extract import _criticality_by_material_plant

    tiers = _criticality_by_material_plant([(m, p) for m in materials for p in plants])
    return {key: tier.value if tier else None for key, tier in tiers.items()}


def test_i07_keeps_each_plants_own_tier(i07_session) -> None:
    """No cross-plant merge: CRITICAL at 1300 and NORMAL at 1500 stages as exactly that."""
    _store(i07_session, [("1", "CRITICAL"), ("2", "NORMAL"), ("3", "INSURANCE")], JUL, plant="1300")
    _store(i07_session, [("1", "NORMAL"), ("2", "OBSOLETE"), ("3", "IMPACT")], JUL, plant="1500")
    tiers = _i07_tiers()
    assert tiers[("1", "1300")] == "CRITICAL" and tiers[("1", "1500")] == "NORMAL"
    assert tiers[("2", "1300")] == "NORMAL" and tiers[("2", "1500")] == "OBSOLETE"
    assert tiers[("3", "1300")] == "INSURANCE" and tiers[("3", "1500")] == "IMPACT"
    assert tiers[("4", "1300")] is None and tiers[("4", "1500")] is None


def test_i07_reads_the_current_values_not_the_history(i07_session) -> None:
    _store(i07_session, [("1", "CRITICAL")], JUL)
    _store(i07_session, [("1", "OBSOLETE")], AUG)
    assert _i07_tiers()[("1", "1300")] == "OBSOLETE"


def test_i07_stages_with_no_criticality_when_nothing_is_loaded(i07_session) -> None:
    assert set(_i07_tiers().values()) == {None}


def test_i07_a_plant_without_a_tier_stays_unknown_not_normal(i07_session) -> None:
    """Only 1500 lists the material: 1300 is None, never defaulted to NORMAL."""
    _store(i07_session, [("2", "IMPACT")], JUL, plant="1500")
    tiers = _i07_tiers()
    assert tiers[("2", "1500")] == "IMPACT" and tiers[("2", "1300")] is None


def test_i07_staged_plant_row_carries_its_own_tier(i07_session) -> None:
    """The column the feature store and the repository read."""
    from app.models.i7_staging import StagedMaterialPlant

    columns = {c.name for c in StagedMaterialPlant.__table__.columns}
    assert "criticality" in columns


# --- the July delivery ----------------------------------------------------

RAW_COLUMNS = "mat_code TEXT, plant TEXT, criticality TEXT, material_description TEXT, mattyp TEXT, mrp_type TEXT"


def _raw(session, table, rows):
    session.execute(text(f"CREATE TABLE {table} ({RAW_COLUMNS})"))
    for row in rows:
        session.execute(text(f"INSERT INTO {table} VALUES (:a, :b, :c, :d, :e, :f)"), dict(zip("abcdef", row)))
    session.commit()


def test_the_july_delivery_is_imported_once_per_plant(session) -> None:
    _raw(session, "raw_zmm065_bmm", [("8000004187", "1300", "NORMAL", "ROCK DRILL", "ZREP", "PD"), ("5", "1300", "", None, None, None)])
    _raw(session, "raw_zmm065_gb", [("5000068389", "1500", "CRITICAL", "REAGENT", "ERSA", "VB")])
    outcome = baseline.import_july_delivery(session)
    assert outcome == {"1300": "imported 2 materials", "1500": "imported 1 materials"}

    entries = {e.upload.plant: e for e in reports.list_uploads(session)}
    gb = entries["1500"].upload
    assert (gb.source, gb.file_name, gb.uploaded_by, gb.report_month, gb.dedupe_key) == (
        "july_delivery", "(July delivery)", None, JUL, "july_delivery:1500",
    )
    assert entries["1300"].status == "current"
    row = session.query(CriticalityCurrent).filter_by(material="8000004187").one()
    assert (row.criticality, row.description, row.mattyp, row.mrp_type) == ("NORMAL", "ROCK DRILL", "ZREP", "PD")

    # Idempotent: a restart, or a second instance, adds nothing.
    assert baseline.import_july_delivery(session) == {
        "1300": "skipped: already imported", "1500": "skipped: already imported",
    }
    assert len(list(reports.list_uploads(session))) == 2


def test_an_upload_for_july_is_not_overwritten_by_the_delivery(session) -> None:
    _raw(session, "raw_zmm065_bmm", [("1", "1300", "NORMAL", None, None, None)])
    _store(session, [("1", "CRITICAL")], JUL)
    assert baseline.import_july_delivery(session)["1300"] == "skipped: a July report was already uploaded"
    assert session.query(CriticalityCurrent).one().criticality == "CRITICAL"


def test_a_later_upload_sits_on_top_of_the_delivery(session) -> None:
    _raw(session, "raw_zmm065_bmm", [("1", "1300", "NORMAL", None, None, None), ("2", "1300", "OBSOLETE", None, None, None)])
    baseline.import_july_delivery(session)
    _store(session, [("1", "CRITICAL")], AUG)
    tiers = {r.material: (r.criticality, r.carried_forward) for r in session.query(CriticalityCurrent)}
    assert tiers == {"1": ("CRITICAL", False), "2": ("OBSOLETE", True)}
    assert {e.upload.report_month: e.status for e in reports.list_uploads(session)} == {AUG: "current", JUL: "previous"}


def test_a_delivered_table_that_is_not_loaded_is_skipped(session) -> None:
    assert baseline.import_july_delivery(session) == {
        "1300": "skipped: raw_zmm065_bmm is not loaded", "1500": "skipped: raw_zmm065_gb is not loaded",
    }


def test_the_delivery_is_read_leniently(session) -> None:
    """Nothing delivered can be refused: another plant's row is dropped, an
    unknown tier is read as blank."""
    _raw(session, "raw_zmm065_bmm", [("1", "1300", "NORMAL", None, None, None), ("2", "1300", "WHATEVER", None, None, None), ("3", "1200", "NORMAL", None, None, None)])
    assert baseline.import_july_delivery(session)["1300"] == "imported 2 materials"
    assert {r.material: r.criticality for r in session.query(CriticalityCurrent)} == {"1": "NORMAL", "2": None}


# --- the fingerprints -----------------------------------------------------


def test_every_initiative_fingerprint_watches_criticality() -> None:
    from app.initiatives.i7.snapshot import I7_RAW_TABLES
    from app.initiatives.i8.service import SOURCE_TABLES
    from app.initiatives.i13.snapshot import FINGERPRINT_TABLES

    for tables in (I7_RAW_TABLES, SOURCE_TABLES, FINGERPRINT_TABLES):
        assert reports.CURRENT_TABLE in tables
        assert "raw_zmm065_bmm" not in tables


def test_an_upload_moves_the_i07_source_fingerprint(session) -> None:
    from app.initiatives.i7.watch import source_fingerprint

    before = source_fingerprint(session)
    _store(session, [("1", "NORMAL")], JUL)
    after = source_fingerprint(session)
    assert before == "" and after.startswith("sha256:")
    _store(session, [("1", "NORMAL")], AUG)
    assert source_fingerprint(session) != after
