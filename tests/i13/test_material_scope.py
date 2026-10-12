"""Material scope: ND/PD/blank -> OAR, VB -> MIN_MAX, no MARC row and everything else -> EXCLUDED."""

import pytest

from app.core.config import get_settings
from app.shared.material_scope import MaterialScope, classify_material_scope


@pytest.mark.parametrize(
    ("dismm", "expected"),
    [
        ("ND", MaterialScope.OAR),
        ("PD", MaterialScope.OAR),
        ("", MaterialScope.OAR),
        ("VB", MaterialScope.MIN_MAX),
        ("V1", MaterialScope.EXCLUDED),
        (None, MaterialScope.EXCLUDED),
        ("unknown", MaterialScope.EXCLUDED),
        ("M0", MaterialScope.EXCLUDED),
    ],
)
def test_classify_material_scope(dismm: str | None, expected: MaterialScope) -> None:
    assert classify_material_scope(dismm) is expected


def test_classify_material_scope_normalises_case_and_whitespace() -> None:
    assert classify_material_scope(" nd ") is MaterialScope.OAR
    assert classify_material_scope("pd") is MaterialScope.OAR
    assert classify_material_scope(" Vb ") is MaterialScope.MIN_MAX


def test_classify_material_scope_blank_whitespace_is_oar() -> None:
    assert classify_material_scope("   ") is MaterialScope.OAR


def test_classify_material_scope_blank_excluded_when_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "i13_oar_include_blank_mrp_type", False)
    assert classify_material_scope("") is MaterialScope.EXCLUDED
    assert classify_material_scope("ND") is MaterialScope.OAR


def test_no_marc_row_is_never_oar() -> None:
    # None is what index.get returns for a pair MARC does not hold.
    assert classify_material_scope(None) is MaterialScope.EXCLUDED
