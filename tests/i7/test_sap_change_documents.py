"""RawChangeDocumentProvider: the real SapStateProvider over raw_cdhdr/raw_cdpos.

Two layers: pure-function tests for identity normalisation and
deduplication (no DB needed), and DB-backed tests against the real seeded
extract confirming what evaluate_conversion_adoption/evaluate_parameter_
adoption actually see through this provider today.
"""

from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations.adoption import (
    evaluate_conversion_adoption,
    evaluate_parameter_adoption,
)
from app.initiatives.i7.recommendations.sap_change_documents import (
    RawChangeDocumentProvider,
    _dedupe,
    _pad_material,
    _rows_for_plant,
)
from app.initiatives.i7.recommendations.types import AdoptionStatus

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


# --- Identity normalisation: I07's unpadded material -> SAP's 18-char MATNR -


def test_pad_material_pads_to_eighteen_characters():
    assert _pad_material("1000000000") == "000000001000000000"
    assert len(_pad_material("1000000000")) == 18


def test_pad_material_strips_surrounding_whitespace_first():
    assert _pad_material("  1000000000  ") == "000000001000000000"


def test_pad_material_leaves_an_already_full_width_value_unchanged():
    full = "0" * 18
    assert _pad_material(full) == full


# --- Deduplication: the full CDPOS composite key, not document_number alone -


def _row(**overrides):
    defaults = dict(
        change_doc_object="MATERIAL",
        object_value="000000001000000000",
        document_number="1",
        date="2026-01-01",
        time="10:00:00",
        table_name="MARC",
        table_key="10000000001300",
        field_name="DISMM",
        change_indicator="U",
        new_value="VB",
        old_value="ND",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_dedupe_drops_an_exact_composite_key_repeat():
    rows = [_row(), _row()]
    assert len(_dedupe(rows)) == 1


def test_dedupe_keeps_rows_that_differ_on_any_key_field():
    rows = [_row(field_name="DISMM"), _row(field_name="EISBE")]
    assert len(_dedupe(rows)) == 2


def test_dedupe_keeps_the_first_occurrence_in_order():
    first = _row(new_value="VB")
    duplicate = _row(new_value="ND")  # same composite key, different value
    assert _dedupe([first, duplicate])[0].new_value == "VB"


def test_dedupe_of_empty_list_is_empty():
    assert _dedupe([]) == []


# --- Plant filtering over table_key --------------------------------------


def test_rows_for_plant_keeps_a_matching_table_key():
    rows = [_row(table_key="10000000001300")]
    assert len(_rows_for_plant(rows, "1300")) == 1


def test_rows_for_plant_drops_a_non_matching_table_key():
    rows = [_row(table_key="10000000001300")]
    assert _rows_for_plant(rows, "1500") == []


def test_rows_for_plant_handles_a_missing_table_key():
    rows = [_row(table_key=None)]
    assert _rows_for_plant(rows, "1300") == []


# --- Against the real, seeded raw_cdhdr/raw_cdpos extract -----------------


@needs_db
@pytest.mark.needs_seed_data
def test_provider_returns_none_when_no_change_document_evidence_exists():
    """Verified fact about the current extract: raw_cdpos has zero MATERIAL/
    MARC rows at all, so every material-plant resolves to no evidence."""
    with get_sessionmaker()() as session:
        provider = RawChangeDocumentProvider(session)
        assert provider.current_state("1000000000", "1300") is None


@needs_db
@pytest.mark.needs_seed_data
def test_provider_returns_none_for_a_material_with_real_cdhdr_history():
    """Material 16622080 has a genuine MATERIAL-class CDHDR document in the
    real extract -- confirming the provider queries real data and still
    correctly finds no MARC/DISMM/EISBE/MINBE/MABST evidence, because CDPOS
    itself carries none for MATERIAL at all."""
    with get_sessionmaker()() as session:
        provider = RawChangeDocumentProvider(session)
        assert provider.current_state("16622080", "1300") is None


@needs_db
@pytest.mark.needs_seed_data
def test_conversion_adoption_through_the_real_provider_is_unknown_not_fabricated():
    """The end-to-end path a real API call takes: no evidence -> UNKNOWN,
    never ADOPTED/NOT_ADOPTED conjured from absent data."""
    with get_sessionmaker()() as session:
        provider = RawChangeDocumentProvider(session)
        result = evaluate_conversion_adoption(
            "1000000000", "1300", expected_mrp_type="VB", provider=provider
        )
        assert result.status is AdoptionStatus.UNKNOWN
        assert "no SAP change-document evidence" in result.detail


@needs_db
@pytest.mark.needs_seed_data
def test_parameter_adoption_through_the_real_provider_is_unknown_not_fabricated():
    with get_sessionmaker()() as session:
        provider = RawChangeDocumentProvider(session)
        result = evaluate_parameter_adoption(
            "1000000000", "1300",
            approved_safety_stock=5, approved_rop=10, approved_max_stock=20,
            provider=provider,
        )
        assert result.status is AdoptionStatus.UNKNOWN


@needs_db
@pytest.mark.needs_seed_data
def test_real_cdhdr_has_material_change_documents_but_cdpos_has_none_for_them():
    """Pins the verified data-gap finding itself, so a future CDPOS delivery
    that actually includes MARC changes is a visible, expected test failure
    here rather than a silent behaviour change discovered downstream."""
    from sqlalchemy import text

    with get_sessionmaker()() as session:
        cdhdr_material_count = session.execute(
            text("select count(*) from raw_cdhdr where change_doc_object = 'MATERIAL'")
        ).scalar()
        cdpos_material_count = session.execute(
            text("select count(*) from raw_cdpos where change_doc_object = 'MATERIAL'")
        ).scalar()
        cdpos_marc_count = session.execute(
            text("select count(*) from raw_cdpos where table_name = 'MARC'")
        ).scalar()

    assert cdhdr_material_count > 0, "expected real MATERIAL change documents in raw_cdhdr"
    assert cdpos_material_count == 0, (
        "raw_cdpos now has MATERIAL rows -- RawChangeDocumentProvider should "
        "start returning real evidence; re-verify the join/table_key "
        "assumptions in sap_change_documents.py against real MARC rows"
    )
    assert cdpos_marc_count == 0
