"""GET /api/i13/validation -- FR-6 reconciliation against ZMM065 and the 30-Day GR Report.

Both reports are read from the serving database (``postgres_reports``) and
compared like with like -- see ``app.initiatives.i13.report_validation`` for
what each report actually measures and why the comparisons are shaped the way
they are:

* ZMM065: the platform's aging band per material-plant, computed as of the
  report's run date, against the report's own class -- per band and per row.
* 30-Day GR Report: every receipt it lists, confirmed against the platform's
  PO lines and goods-receipt postings.

A report that is not loaded yields its result rows as REFERENCE_UNAVAILABLE and
its detail block as null.

The two ``*_reference_count`` query parameters predate the reports being
loaded, when a person typed the counts in. They are still accepted so an older
client does not error, and ignored: a typed count was compared against figures
that were not the same kind of thing (see the module above).
"""

from collections import defaultdict
from dataclasses import asdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.i13.deps import snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.movements import ISSUE_TYPES, RECEIPT_TYPES, last_unreversed_date, reversal_types_for
from app.initiatives.i13.procurement_chain import build_procurement_chain
from app.initiatives.i13.report_validation import (
    Gr30DayValidation,
    Zmm065Validation,
    validate_gr_30day,
    validate_zmm065,
)
from app.initiatives.i13.snapshot import I13Snapshot
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reports import fetch_gr_30day_rows, fetch_zmm065_rows
from app.schemas.i13 import (
    Gr30DayValidationResponse,
    ReconciliationSourceResult,
    ValidationResponse,
    Zmm065ValidationResponse,
)

router = APIRouter()

Key = tuple[str, str]


@router.get("/validation", response_model=ValidationResponse)
def get_validation(
    zmm065_reference_count: int | None = Query(
        None, description="Deprecated and ignored: ZMM065 is now read from the database."
    ),
    gr_30_day_reference_count: int | None = Query(
        None, description="Deprecated and ignored: the 30-Day GR Report is now read from the database."
    ),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> ValidationResponse:
    tolerance = config.reconciliation.tolerance_pct
    if snapshot is not None:
        issue_events, history_start = snapshot.issue_events, snapshot.movement_history_start
        po_line_plant = _po_line_plant(snapshot.procurement_chain)
        receipt_dates = snapshot.receipt_dates_by_po_line
    else:
        issue_events, history_start, po_line_plant, receipt_dates = _live_inputs(db)

    def last_issue_as_of(key: Key, day: date) -> date | None:
        return last_unreversed_date(list(issue_events.get(key, ())), ISSUE_TYPES, as_of=day)

    zmm065_rows = fetch_zmm065_rows(db)
    zmm065 = (
        validate_zmm065(
            zmm065_rows,
            last_issue_as_of=last_issue_as_of,
            history_start=history_start,
            thresholds=config.aging,
            tolerance_pct=tolerance,
        )
        if zmm065_rows is not None
        else None
    )
    gr_rows = fetch_gr_30day_rows(db)
    gr = (
        validate_gr_30day(gr_rows, po_line_plant=po_line_plant, receipt_dates=receipt_dates, tolerance_pct=tolerance)
        if gr_rows is not None
        else None
    )

    results = (
        list(zmm065.results)
        if zmm065
        else [_unavailable(f"ZMM065 · {band}") for band in ("Fast moving", "Slow moving", "Non-moving")]
    )
    results.append(gr.result if gr else _unavailable("30-Day GR Report · receipts confirmed"))
    return ValidationResponse(
        tolerance_pct=Decimal(str(tolerance)),
        results=[ReconciliationSourceResult.model_validate(r) for r in results],
        zmm065=_zmm065_response(zmm065) if zmm065 else None,
        gr_30_day=_gr_response(gr) if gr else None,
    )


def _po_line_plant(procurement_chain) -> dict[Key, str]:
    return {
        (entry.po_number, entry.po_item): entry.plant
        for entry in procurement_chain
        if entry.po_number and entry.po_item
    }


def _live_inputs(db: Session):
    """The snapshot's validation inputs, read on demand (``?live=true`` or the
    snapshot switched off). Slow on real data, as every live I13 route is."""
    movement_repo = PostgresMovementRepository(db)
    procurement_repo = PostgresProcurementRepository(db)
    wanted = set(ISSUE_TYPES) | reversal_types_for(ISSUE_TYPES)
    issue_events: dict[Key, list] = defaultdict(list)
    history_start: date | None = None
    for row in movement_repo.get_movement_history():
        moved_on = row.get("BudatMkpf")
        if moved_on is not None and (history_start is None or moved_on < history_start):
            history_start = moved_on
        if row.get("Bwart") in wanted:
            issue_events[(row["Matnr"], row["Werks"])].append(row)
    receipt_dates: dict[Key, set[date]] = defaultdict(set)
    for row in procurement_repo.get_goods_receipt_history():
        if row.get("Bwart") in RECEIPT_TYPES and row.get("BudatMkpf") is not None:
            receipt_dates[(row["Ebeln"], row["Ebelp"])].add(row["BudatMkpf"])
    po_line_plant = _po_line_plant(build_procurement_chain(procurement_repo))
    return issue_events, history_start, po_line_plant, receipt_dates


def _unavailable(source_name: str) -> dict:
    return {
        "source_name": source_name,
        "computed_count": 0,
        "reference_count": None,
        "absolute_difference": None,
        "percentage_difference": None,
        "within_tolerance": None,
        "status": "REFERENCE_UNAVAILABLE",
    }


def _zmm065_response(result: Zmm065Validation) -> Zmm065ValidationResponse:
    return Zmm065ValidationResponse(
        report_date=result.report_date,
        rows_in_report=result.rows_in_report,
        compared=result.compared,
        agreed=result.agreed,
        agreement_pct=result.agreement_pct,
        excluded_non_aging=dict(result.excluded_non_aging),
        mismatch_reasons=dict(result.mismatch_reasons),
        mismatches=[
            {
                **asdict(m),
                "report_band": m.report_band.value,
                "platform_band": m.platform_band.value,
                "reason": m.reason.value,
            }
            for m in result.mismatches
        ],
    )


def _gr_response(result: Gr30DayValidation) -> Gr30DayValidationResponse:
    return Gr30DayValidationResponse(
        report_date=result.report_date,
        window_start=result.window_start,
        rows_in_report=result.rows_in_report,
        confirmed=result.confirmed,
        plants=dict(result.plants),
        platform_receipts_in_window=result.platform_receipts_in_window,
        unconfirmed=[{**asdict(c), "status": c.status.value} for c in result.unconfirmed],
    )
