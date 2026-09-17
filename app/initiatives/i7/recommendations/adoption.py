"""Read-only SAP adoption reconciliation.

Formula Reference::

    Adopted if:
        MRP_TYPE = VB
    AND MINBE is populated
    AND MABST is populated

    I07 reads SAP change history (CDHDR/CDPOS) to measure adoption after VZI
    manual execution. It does not write to SAP.

**Evidence comes through an interface, never a raw query.** Phase 2's staging
layer never materialised CDHDR/CDPOS -- it was scoped to materials, consumption
and purchase orders -- so there is no staged table to read today. This module
therefore takes a :class:`SapStateProvider` rather than opening a session onto
``raw_cdhdr``/``raw_cdpos`` itself: querying those tables directly from the
recommendation package would coyly reintroduce the SAP coupling the whole
adapter architecture exists to prevent, just one layer further from where
anyone would look for it. Until a provider exists, evidence is genuinely
absent, and the honest answer is UNKNOWN.

**Unchanged is not the same as proven unchanged.** With no baseline the
question "did SAP change?" cannot be answered either way, and reporting
NOT_ADOPTED would assert a fact nobody observed. NOT_ADOPTED is reserved for the
case where current SAP state was actually read and does not match what was
approved.
"""

from typing import Protocol

from app.initiatives.i7.recommendations.types import AdoptionResult, AdoptionStatus


class SapStateProvider(Protocol):
    """Current SAP planning parameters for one material-plant, read-only.

    Returns ``None`` when no evidence exists at all -- there is no staged
    CDHDR/CDPOS today, so every concrete call site uses the ``None`` path,
    and the interface exists so a future adapter can be substituted without
    touching this module.
    """

    def current_state(self, material: str, plant: str) -> dict[str, str] | None: ...


class NoSapStateAvailable:
    """The only provider that exists today: there is no staged evidence."""

    def current_state(self, material: str, plant: str) -> dict[str, str] | None:
        return None


def evaluate_conversion_adoption(
    material: str,
    plant: str,
    expected_mrp_type: str,
    provider: SapStateProvider | None = None,
) -> AdoptionResult:
    """Compare an approved OAR->Min-Max conversion against current SAP state.

    Expected: MRP type = ``expected_mrp_type`` (VB, per policy) and MINBE and
    MABST both populated.
    """
    provider = provider or NoSapStateAvailable()
    expected = (
        ("mrp_type", expected_mrp_type),
        ("minbe_populated", "true"),
        ("mabst_populated", "true"),
    )

    observed_state = provider.current_state(material, plant)
    if observed_state is None:
        return AdoptionResult(
            status=AdoptionStatus.UNKNOWN,
            expected=expected,
            observed=(),
            matched_fields=(),
            mismatched_fields=(),
            detail="no SAP change-document evidence is available for this material-plant",
        )

    observed_mrp = observed_state.get("mrp_type")
    minbe = observed_state.get("minbe")
    mabst = observed_state.get("mabst")

    minbe_populated = bool(minbe) and minbe not in ("0", "0.00")
    mabst_populated = bool(mabst) and mabst not in ("0", "0.00")

    checks = (
        ("mrp_type", observed_mrp == expected_mrp_type),
        ("minbe_populated", minbe_populated),
        ("mabst_populated", mabst_populated),
    )
    matched = tuple(name for name, ok in checks if ok)
    mismatched = tuple(name for name, ok in checks if not ok)

    observed = (
        ("mrp_type", observed_mrp or ""),
        ("minbe_populated", str(minbe_populated).lower()),
        ("mabst_populated", str(mabst_populated).lower()),
    )

    if not mismatched:
        status = AdoptionStatus.ADOPTED
        detail = "SAP state matches the approved conversion in full"
    elif matched:
        status = AdoptionStatus.PARTIALLY_ADOPTED
        detail = f"matched {', '.join(matched)}; differs on {', '.join(mismatched)}"
    else:
        status = AdoptionStatus.NOT_ADOPTED
        detail = "SAP state matches none of the expected fields"

    return AdoptionResult(
        status=status,
        expected=expected,
        observed=observed,
        matched_fields=matched,
        mismatched_fields=mismatched,
        detail=detail,
    )


def evaluate_parameter_adoption(
    material: str,
    plant: str,
    approved_safety_stock: int | None,
    approved_rop: int | None,
    approved_max_stock: int | None,
    provider: SapStateProvider | None = None,
) -> AdoptionResult:
    """Compare an approved normal-path parameter change against current SAP
    state, by the same principle as the conversion check."""
    provider = provider or NoSapStateAvailable()

    expected = tuple(
        (name, str(value))
        for name, value in (
            ("safety_stock", approved_safety_stock),
            ("reorder_point", approved_rop),
            ("maximum_stock", approved_max_stock),
        )
        if value is not None
    )

    if not expected:
        return AdoptionResult(
            status=AdoptionStatus.UNKNOWN,
            expected=(),
            observed=(),
            matched_fields=(),
            mismatched_fields=(),
            detail="no approved parameter values exist to reconcile against",
        )

    observed_state = provider.current_state(material, plant)
    if observed_state is None:
        return AdoptionResult(
            status=AdoptionStatus.UNKNOWN,
            expected=expected,
            observed=(),
            matched_fields=(),
            mismatched_fields=(),
            detail="no SAP change-document evidence is available for this material-plant",
        )

    field_map = {
        "safety_stock": "eisbe",
        "reorder_point": "minbe",
        "maximum_stock": "mabst",
    }
    checks = []
    observed = []
    for name, expected_value in expected:
        sap_field = field_map[name]
        observed_value = observed_state.get(sap_field)
        observed.append((name, observed_value or ""))
        checks.append((name, observed_value == expected_value))

    matched = tuple(name for name, ok in checks if ok)
    mismatched = tuple(name for name, ok in checks if not ok)

    if not mismatched:
        status = AdoptionStatus.ADOPTED
        detail = "SAP values match the approved recommendation in full"
    elif matched:
        status = AdoptionStatus.PARTIALLY_ADOPTED
        detail = f"matched {', '.join(matched)}; differs on {', '.join(mismatched)}"
    else:
        status = AdoptionStatus.NOT_ADOPTED
        detail = "SAP values match none of the approved parameters"

    return AdoptionResult(
        status=status,
        expected=expected,
        observed=tuple(observed),
        matched_fields=matched,
        mismatched_fields=mismatched,
        detail=detail,
    )
