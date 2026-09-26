"""Lead-time source selection: Initiative 11 first, MARC-PLIFZ fallback.

Pure functions and fake providers throughout -- no database, no real
Initiative 11 integration exists to test against, and none is fabricated here.
"""

from decimal import Decimal

from app.initiatives.i7.contracts import MaterialIdentity, MaterialPlantKey, PlantIdentity
from app.initiatives.i7.contracts.enums import LeadTimeSource
from app.initiatives.i7.features.lead_time_provider import (
    I11LeadTimeProvider,
    LeadTimeProviderResult,
    MarcPlifzLeadTimeProvider,
    resolve_lead_time,
)

KEY = MaterialPlantKey(
    material=MaterialIdentity(sap_material_number="000000000010000000"),
    plant=PlantIdentity(sap_plant_code="1300"),
)


class _FakeI11Provider:
    """A stand-in for the real Initiative 11 integration, once it exists."""

    def __init__(self, result: LeadTimeProviderResult | None) -> None:
        self._result = result

    def get(self, key: MaterialPlantKey) -> LeadTimeProviderResult | None:
        return self._result


# --- I11 available -----------------------------------------------------------


def test_i11_result_is_used_when_available():
    i11_result = LeadTimeProviderResult(
        source=LeadTimeSource.I11_PROGRAM,
        lead_time_days=Decimal("12"),
        detail="Initiative 11 Z-program output",
    )
    result = resolve_lead_time(
        KEY, planned_delivery_time_days=30, i11_provider=_FakeI11Provider(i11_result)
    )
    assert result.source is LeadTimeSource.I11_PROGRAM
    assert result.lead_time_days == Decimal("12")


def test_i11_result_wins_even_when_marc_plifz_would_also_answer():
    """I11 is preferred outright, not just when MARC-PLIFZ is unavailable."""
    i11_result = LeadTimeProviderResult(
        source=LeadTimeSource.I11_PROGRAM,
        lead_time_days=Decimal("9"),
        detail="Initiative 11 Z-program output",
    )
    result = resolve_lead_time(
        KEY, planned_delivery_time_days=45, i11_provider=_FakeI11Provider(i11_result)
    )
    assert result.source is LeadTimeSource.I11_PROGRAM
    assert result.lead_time_days == Decimal("9")


# --- I11 unavailable -> MARC-PLIFZ fallback ----------------------------------


def test_marc_plifz_fallback_when_i11_unavailable():
    """The real I11LeadTimeProvider always reports unavailable today."""
    result = resolve_lead_time(KEY, planned_delivery_time_days=30)
    assert result.source is LeadTimeSource.PLANNED_DELIVERY_TIME
    assert result.lead_time_days == Decimal("30")


def test_real_i11_provider_never_fabricates_a_value():
    assert I11LeadTimeProvider().get(KEY) is None


def test_marc_plifz_provider_returns_none_below_one_day():
    assert MarcPlifzLeadTimeProvider().get(KEY, planned_delivery_time_days=0) is None
    assert MarcPlifzLeadTimeProvider().get(KEY, planned_delivery_time_days=-5) is None


# --- Missing / null lead-time data --------------------------------------------


def test_no_source_available_reports_null_lead_time_not_zero():
    """Neither I11 nor a usable MARC-PLIFZ value: null, not a fabricated zero."""
    result = resolve_lead_time(KEY, planned_delivery_time_days=None)
    assert result.lead_time_days is None
    assert result.source is LeadTimeSource.PLANNED_DELIVERY_TIME
    assert "no Initiative 11 output" in result.detail


def test_negative_planned_delivery_time_is_not_treated_as_a_value():
    result = resolve_lead_time(KEY, planned_delivery_time_days=-1)
    assert result.lead_time_days is None
