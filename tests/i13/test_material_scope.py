"""Material scope: MRP controller (DISPO) OAR -> OAR; blank, any other code and no MARC row -> EXCLUDED."""

import pytest

from app.core.config import get_settings
from app.shared.material_scope import MaterialScope, classify_material_scope


@pytest.mark.parametrize(
    ("dispo", "expected"),
    [
        ("OAR", MaterialScope.OAR),
        # Codes measured on QA, 20 Oct 2026 -- none of them is OAR.
        ("POD", MaterialScope.EXCLUDED),
        ("REP", MaterialScope.EXCLUDED),
        ("OBS", MaterialScope.EXCLUDED),
        ("101", MaterialScope.EXCLUDED),
        ("MIN", MaterialScope.EXCLUDED),
        # The old MRP-type codes are not controllers and are not OAR either.
        ("ND", MaterialScope.EXCLUDED),
        ("PD", MaterialScope.EXCLUDED),
        ("", MaterialScope.EXCLUDED),
        (None, MaterialScope.EXCLUDED),
    ],
)
def test_classify_material_scope(dispo: str | None, expected: MaterialScope) -> None:
    assert classify_material_scope(dispo) is expected


def test_classify_material_scope_normalises_case_and_whitespace() -> None:
    assert classify_material_scope(" oar ") is MaterialScope.OAR
    assert classify_material_scope("Oar") is MaterialScope.OAR


def test_blank_dispo_is_not_oar() -> None:
    assert classify_material_scope("   ") is MaterialScope.EXCLUDED


def test_min_max_is_no_longer_produced() -> None:
    for code in ("OAR", "POD", "VB", "", None):
        assert classify_material_scope(code) is not MaterialScope.MIN_MAX


def test_the_controller_list_is_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "i13_oar_mrp_controllers", "OAR, pod")
    assert classify_material_scope("POD") is MaterialScope.OAR
    assert classify_material_scope("OAR") is MaterialScope.OAR
    assert classify_material_scope("REP") is MaterialScope.EXCLUDED


def test_no_marc_row_is_never_oar() -> None:
    # None is what index.get returns for a pair MARC does not hold.
    assert classify_material_scope(None) is MaterialScope.EXCLUDED
