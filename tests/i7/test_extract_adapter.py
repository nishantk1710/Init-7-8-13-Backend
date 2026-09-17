"""Extract adapter: parsing, policy and mapping.

Unit-level. Nothing here needs a database -- these cover the value-level
decisions the adapter makes, which is where a mapping bug actually lives.
Database-backed mapping proof is in ``test_staging.py``.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.initiatives.i7.adapters import ConsumptionMovementPolicy, ExtractIngestionPolicy
from app.initiatives.i7.adapters.extract import _safe_batch_size
from app.initiatives.i7.adapters.field_map import (
    CREDIT_INDICATOR,
    EKBE_FIELDS,
    GOODS_RECEIPT_HISTORY_CATEGORY,
    MARA_FIELDS,
    MARC_FIELDS,
    MARC_MISSING_FIELDS,
    MSEG_FIELDS,
)
from app.initiatives.i7.adapters.validation import (
    RejectionReason,
    clean,
    parse_date,
    parse_decimal,
    parse_flag,
    parse_int,
    parse_purchasing_deletion,
)
from app.models.i7_staging import StagedMaterial, StagedPurchaseOrder


# --- Parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("  PD  ", "PD"), ("", None), ("   ", None), (None, None), ("ND", "ND")],
)
def test_clean_treats_blank_as_absent(raw, expected):
    assert clean(raw) == expected


def test_clean_accepts_non_string_values():
    """SQL aggregates arrive as Decimal, not str."""
    assert clean(Decimal("12.5")) == "12.5"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-03-17", date(2026, 3, 17)),
        ("", None),
        (None, None),
        ("00000000", None),
        ("0000-00-00", None),
        ("not-a-date", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4", Decimal("4")),
        ("2766.096", Decimal("2766.096")),
        ("1,234.5", Decimal("1234.5")),
        ("-3", Decimal("-3")),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_decimal(raw, expected):
    assert parse_decimal(raw) == expected


def test_parse_decimal_keeps_precision():
    """Decimal, not float -- 0.1 has no exact binary representation."""
    assert parse_decimal("0.1") + parse_decimal("0.2") == Decimal("0.3")


@pytest.mark.parametrize("raw,expected", [("14", 14), ("14.0", 14), ("", None), ("x", None)])
def test_parse_int_tolerates_a_decimal_point(raw, expected):
    assert parse_int(raw) == expected


@pytest.mark.parametrize("raw,expected", [("X", True), ("x", True), ("", False), (None, False)])
def test_parse_flag(raw, expected):
    assert parse_flag(raw) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [("L", True), ("S", True), ("l", True), ("", False), (None, False), ("X", False)],
)
def test_purchasing_deletion_is_a_code_not_a_boolean(raw, expected):
    """EKPO.LOEKZ holds ``L`` (deleted) or ``S`` (blocked), never ``X``.

    Regression: reading it with :func:`parse_flag` marked all 13,315 deleted and
    blocked lines in the extract as active, which would have admitted cancelled
    orders into the lead-time population the Formula Reference excludes.
    """
    assert parse_purchasing_deletion(raw) is expected


def test_lvorm_and_loekz_use_different_parsers():
    """The two SAP deletion fields genuinely differ in encoding."""
    assert parse_flag("X") is True and parse_purchasing_deletion("X") is False
    assert parse_flag("L") is False and parse_purchasing_deletion("L") is True


# --- Movement-type policy ---------------------------------------------


def test_movement_policy_is_not_confirmed():
    """The FRS calls the consumption movement set an unconfirmed constant."""
    assert ConsumptionMovementPolicy().confirmed is False


def test_issues_count_positive_and_reversals_negative():
    policy = ConsumptionMovementPolicy()
    assert policy.signed_multiplier("201") == 1
    assert policy.signed_multiplier("261") == 1
    assert policy.signed_multiplier("202") == -1
    assert policy.signed_multiplier("262") == -1


def test_unlisted_movement_type_contributes_nothing():
    """311 transfers and 641 deliveries are movements, but not demand."""
    policy = ConsumptionMovementPolicy()
    for movement in ("101", "311", "641", "543"):
        assert policy.signed_multiplier(movement) == 0


def test_movement_types_are_configurable():
    policy = ConsumptionMovementPolicy(
        issue_movement_types=("201", "261", "311"), reversal_movement_types=("202",)
    )
    assert policy.signed_multiplier("311") == 1
    assert set(policy.all_movement_types) == {"201", "261", "311", "202"}


def test_all_movement_types_covers_issues_and_reversals():
    assert set(ConsumptionMovementPolicy().all_movement_types) == {"201", "261", "202", "262"}


# --- Field map ---------------------------------------------------------


def test_field_map_records_all_three_vocabularies():
    """Extract label, SAP field and canonical name are genuinely different."""
    mapping = {field.canonical_name: field for field in MARC_FIELDS}
    mrp = mapping["mrp_type"]
    assert (mrp.raw_column, mrp.sap_field) == ("mrp_type", "DISMM")
    plifz = mapping["planned_delivery_time_days"]
    assert (plifz.raw_column, plifz.sap_field) == ("planned_deliv_time", "PLIFZ")


def test_mstae_and_extwg_both_mapped_from_mara():
    mapping = {field.canonical_name: field.sap_field for field in MARA_FIELDS}
    assert mapping["material_status"] == "MSTAE"
    assert mapping["external_material_group"] == "EXTWG"


def test_safety_stock_is_recorded_as_absent_from_the_extract():
    """EISBE is required by the FRS but not delivered in the MARC export."""
    missing = {field.sap_field for field in MARC_MISSING_FIELDS}
    assert "EISBE" in missing
    assert "current_safety_stock" not in {field.canonical_name for field in MARC_FIELDS}


def test_mseg_maps_the_sign_indicator():
    """Without SHKZG a reversal would add to demand instead of cancelling it."""
    assert "debit_credit_indicator" in {field.canonical_name for field in MSEG_FIELDS}


def test_goods_receipt_constants_are_named_not_inlined():
    assert GOODS_RECEIPT_HISTORY_CATEGORY == "E"
    assert CREDIT_INDICATOR == "H"
    assert "history_category" in {field.canonical_name for field in EKBE_FIELDS}


# --- Batching ----------------------------------------------------------


def test_batch_size_stays_under_the_parameter_ceiling():
    """Postgres caps one statement at 65,535 bind parameters."""
    for model in (StagedMaterial, StagedPurchaseOrder):
        columns = len(model.__table__.columns)
        assert _safe_batch_size(model, 5000) * columns <= 65535


def test_batch_size_never_drops_below_one():
    assert _safe_batch_size(StagedMaterial, 1) == 1


def test_default_policy_batches_rather_than_loading_everything():
    assert ExtractIngestionPolicy().batch_size <= 10000


# --- Rejection reasons -------------------------------------------------


def test_rejection_reasons_are_stable_codes():
    """These are grouped in SQL, so they must be codes rather than prose."""
    assert RejectionReason.MISSING_MATERIAL == "missing_material"
    assert RejectionReason.RECEIPT_BEFORE_CREATION == "receipt_before_creation"
    assert " " not in RejectionReason.INVALID_QUANTITY
