"""GET /api/i13/grni, GET /api/i13/usage-patterns.

The two OAR screens that never had a backend: the 30-day goods-received-not-
issued list and the month-by-month usage pattern. Both are served from the I13
snapshot only (``app.initiatives.i13.snapshot``) -- there is no live path,
because neither existed before the snapshot and both would be a full-tenant
rebuild per request without it.

* **GRNI** applies the rule WATCH already applies per material-plant
  (``watch._gr_not_issued``: received - issued > 0 and the last GR at least
  ``I13_GR_NOT_ISSUED_THRESHOLD_DAYS`` old), per reservation-ledger entry.
  FRS FR-6; no new rule.
* **Usage patterns** are the net monthly goods issues and receipts from the
  same reversal-aware movement netting WATCH's consumption uses. The history
  is only as deep as the delivered MSEG extract (blocker B4), so the response
  says which months it covers rather than implying twelve.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.i13.deps import page, require_snapshot
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.snapshot import GrniEntry, I13Snapshot, MonthlyConsumption
from app.schemas.i13 import GrniEntryResponse, MonthlyConsumptionResponse, UsagePatternResponse
from app.shared.material_scope import MaterialScope

router = APIRouter()


def _require_snapshot() -> I13Snapshot:
    """These routes have no live path, so they serve the snapshot even when
    ``I13_SNAPSHOT_ENABLED`` is off for the others."""
    return require_snapshot()


def _grni_response(item: GrniEntry, threshold_days: int) -> GrniEntryResponse:
    entry = item.ledger
    return GrniEntryResponse(
        ledger_id=entry.ledger_id,
        reservation_number=entry.reservation_number,
        reservation_item=entry.reservation_item,
        material=entry.material,
        plant=entry.plant,
        material_scope=entry.material_scope.value,
        pr_number=entry.pr_number,
        po_number=entry.po_number,
        po_item=entry.po_item,
        received_quantity=entry.received_quantity or Decimal("0"),
        issued_quantity=entry.issued_quantity,
        outstanding_quantity=item.outstanding_quantity,
        first_gr_date=entry.first_gr_date,
        last_gr_date=entry.last_gr_date,
        days_since_gr=item.days_since_gr,
        threshold_days=threshold_days,
        requirement_date=entry.requirement_date,
        lifecycle_status=entry.lifecycle_status.value,
    )


@router.get("/grni", response_model=list[GrniEntryResponse])
def list_grni(
    response: Response,
    plant: str | None = Query(None),
    material: str | None = Query(None),
    min_days: int | None = Query(None, ge=0, description="Only entries at least this many days since GR."),
    include_out_of_scope: bool = Query(False, description="Include non-OAR (Min-Max/Excluded) materials."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    snapshot: I13Snapshot = Depends(_require_snapshot),
    config: I13Config = Depends(get_i13_config),
) -> list[GrniEntryResponse]:
    """Oldest goods receipt first. ``X-Total-Count`` carries the full count."""
    items = [
        item
        for item in snapshot.grni_entries
        if (include_out_of_scope or item.ledger.material_scope is MaterialScope.OAR)
        and (not plant or item.ledger.plant == plant)
        and (not material or item.ledger.material == material)
        and (min_days is None or item.days_since_gr >= min_days)
    ]
    threshold = config.watch.gr_not_issued_threshold_days
    return [_grni_response(item, threshold) for item in page(items, response, limit=limit, offset=offset)]


def _filled_months(series: tuple[MonthlyConsumption, ...], months: tuple[str, ...]) -> list[MonthlyConsumption]:
    by_month = {m.month: m for m in series}
    zero = Decimal("0")
    return [by_month.get(month) or MonthlyConsumption(month, zero, 0, zero) for month in months]


def _usage_response(snapshot: I13Snapshot, key: tuple[str, str]) -> UsagePatternResponse:
    material, plant = key
    series = snapshot.monthly_consumption.get(key, ())
    watch = snapshot.watch.get(key)
    issued_months = [m for m in series if m.issued_quantity > 0]
    return UsagePatternResponse(
        material=material,
        plant=plant,
        material_scope=snapshot.scope_of(material, plant).value,
        aging_band=watch.aging_band.value if watch else None,
        stock_on_hand=watch.stock_on_hand if watch else snapshot.stock_by_key.get(key),
        average_monthly_consumption=watch.average_monthly_consumption if watch else None,
        months_of_cover=watch.months_of_cover if watch else None,
        issued_quantity_total=sum((m.issued_quantity for m in series), Decimal("0")),
        issue_count_total=sum(m.issue_count for m in series),
        active_months=len(issued_months),
        last_issue_month=issued_months[-1].month if issued_months else None,
        months=[
            MonthlyConsumptionResponse.model_validate(m) for m in _filled_months(series, snapshot.history_months)
        ],
    )


@router.get("/usage-patterns", response_model=list[UsagePatternResponse])
def list_usage_patterns(
    response: Response,
    plant: str | None = Query(None),
    material: str | None = Query(None),
    aging_band: str | None = Query(None),
    include_out_of_scope: bool = Query(False, description="Include non-OAR (Min-Max/Excluded) materials."),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    snapshot: I13Snapshot = Depends(_require_snapshot),
) -> list[UsagePatternResponse]:
    """Material-plants with any movement history, most issued first.

    ``X-I13-History-Months`` names the first and last month the delivered
    movement history covers (e.g. ``2025-08..2026-08``).
    """
    wanted_band = aging_band.upper() if aging_band else None
    keys = [
        key
        for key in snapshot.monthly_consumption
        if (not plant or key[1] == plant)
        and (not material or key[0] == material)
        and (include_out_of_scope or snapshot.scope_of(*key) is MaterialScope.OAR)
        and (
            wanted_band is None
            or ((w := snapshot.watch.get(key)) is not None and w.aging_band.value == wanted_band)
        )
    ]
    issued_total = {
        key: sum((m.issued_quantity for m in snapshot.monthly_consumption[key]), Decimal("0")) for key in keys
    }
    keys.sort(key=lambda k: (-issued_total[k], k))
    months = snapshot.history_months
    if months:
        response.headers["X-I13-History-Months"] = f"{months[0]}..{months[-1]}"
    return [_usage_response(snapshot, key) for key in page(keys, response, limit=limit, offset=offset)]


@router.get("/usage-patterns/{material}/{plant}", response_model=UsagePatternResponse)
def get_usage_pattern(
    material: str, plant: str, snapshot: I13Snapshot = Depends(_require_snapshot)
) -> UsagePatternResponse:
    key = (material, plant)
    if key not in snapshot.monthly_consumption and key not in snapshot.watch:
        raise HTTPException(status_code=404, detail="No movement history for this material/plant")
    return _usage_response(snapshot, key)
