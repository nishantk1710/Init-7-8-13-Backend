"""Material scope: MRP type (DISMM) ND or PD -> OAR; blank, any other type and no MARC row -> EXCLUDED.

The I13 ruling of 21 Nov 2026. DISPO is not read at all.
"""

import pytest

from app.core.config import get_settings
from app.shared.material_scope import MaterialScope, build_scope_index, classify_material_scope


@pytest.mark.parametrize(
    ("dismm", "expected"),
    [
        ("ND", MaterialScope.OAR),
        ("PD", MaterialScope.OAR),
        # Reorder-point planned (Min-Max) and the undocumented codes are not OAR.
        ("VB", MaterialScope.EXCLUDED),
        ("V1", MaterialScope.EXCLUDED),
        ("M0", MaterialScope.EXCLUDED),
        # The old MRP controller code is not an MRP type and is not OAR either.
        ("OAR", MaterialScope.EXCLUDED),
        # Blank DISMM is not OAR (it was under the September rule).
        ("", MaterialScope.EXCLUDED),
        (None, MaterialScope.EXCLUDED),
    ],
)
def test_classify_material_scope(dismm: str | None, expected: MaterialScope) -> None:
    assert classify_material_scope(dismm) is expected


def test_classify_material_scope_normalises_case_and_whitespace() -> None:
    assert classify_material_scope(" nd ") is MaterialScope.OAR
    assert classify_material_scope("Pd") is MaterialScope.OAR


def test_blank_dismm_is_not_oar() -> None:
    assert classify_material_scope("   ") is MaterialScope.EXCLUDED


def test_min_max_is_no_longer_produced() -> None:
    for code in ("ND", "PD", "VB", "", None):
        assert classify_material_scope(code) is not MaterialScope.MIN_MAX


def test_the_mrp_type_list_is_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "i13_oar_mrp_types", "ND")
    assert classify_material_scope("ND") is MaterialScope.OAR
    assert classify_material_scope("PD") is MaterialScope.EXCLUDED


def test_no_marc_row_is_never_oar() -> None:
    # None is what index.get returns for a pair MARC does not hold.
    assert classify_material_scope(None) is MaterialScope.EXCLUDED


def test_odata_scope_index_reads_dismm_not_dispo() -> None:
    index = build_scope_index(
        [
            {"Matnr": "1", "Werks": "1300", "Dismm": "ND", "Dispo": "MIN"},
            {"Matnr": "2", "Werks": "1300", "Dismm": "VB", "Dispo": "OAR"},
        ]
    )
    assert index[("1", "1300")] is MaterialScope.OAR
    assert index[("2", "1300")] is MaterialScope.EXCLUDED
