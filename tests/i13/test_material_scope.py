"""Material scope: ND/PD -> OAR, VB -> MIN_MAX, everything else -> EXCLUDED."""

import pytest

from app.shared.material_scope import MaterialScope, classify_material_scope


@pytest.mark.parametrize(
    ("dismm", "expected"),
    [
        ("ND", MaterialScope.OAR),
        ("PD", MaterialScope.OAR),
        ("VB", MaterialScope.MIN_MAX),
        ("V1", MaterialScope.EXCLUDED),
        ("", MaterialScope.EXCLUDED),
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


def test_classify_material_scope_blank_whitespace_is_excluded() -> None:
    assert classify_material_scope("   ") is MaterialScope.EXCLUDED
