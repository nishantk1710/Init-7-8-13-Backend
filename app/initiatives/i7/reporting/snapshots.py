"""Capturing the monthly KPI snapshots (``i7_kpi_snapshot``).

A snapshot is the month's position, measured with the same definitions as everything else in the report
(:mod:`kpi_definitions`) -- this module adds no business logic of its own, it only freezes the result. See
``app/models/i7_kpi_snapshot.py`` for what a snapshot is, what it speaks for, and the OPEN / FINAL lifecycle.

**Idempotent.** One row per month. Capturing the same month with the same figures changes nothing: no new row, no new
revision, no log entry. Capturing it with different figures while it is OPEN updates the row (revision + 1, logged).

**A final snapshot is not replaced silently.** Once a snapshot has been captured after its month ended it is FINAL.
Capturing it again is REFUSED (logged) unless ``force`` is given, and a forced replacement is logged as
REPLACED_FINAL with the fingerprint it replaced. Nothing writes over a published month without leaving that record.

**Failures are logged and retryable.** If the figures cannot be computed the failure is written to the log in its own
transaction and the error is raised, so a command exits non-zero and a scheduler can try again; nothing partial is
saved, and the previous snapshot is left exactly as it was.

**Not a reconstruction.** A month that was not captured has no snapshot. This module never builds one for a past month
from today's data: the stockout usage holds no history and stock on hand is only known as it is now, so doing that
would invent a measurement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.reporting.data_coverage import _latest_issue_date, _stock_loaded_at
from app.initiatives.i7.reporting.kpi_definitions import MAX_MEASUREMENT_AGE_DAYS
from app.initiatives.i7.reporting.period import exclusive_end_of, reporting_cutoff
from app.models.i7_kpi_snapshot import STATUS_FINAL, STATUS_OPEN, KpiSnapshot, KpiSnapshotLog

logger = get_logger(__name__)

#: Bumped when the way a snapshot's figures are calculated changes, so two snapshots are never compared across it blindly.
#: "2": the working capital impact is MBEW's price and WAERS stated in ZAR (ZAR, and NAD at 1:1), not every priced row.
CALC_VERSION = "2"

CREATED = "CREATED"
UPDATED = "UPDATED"
FINALISED = "FINALISED"
UNCHANGED = "UNCHANGED"
REFUSED = "REFUSED"
REPLACED_FINAL = "REPLACED_FINAL"
FAILED = "FAILED"


@dataclass(frozen=True)
class CaptureResult:
    month: date
    action: str
    snapshot: KpiSnapshot | None
    detail: str = ""


def first_of_month(value: date) -> date:
    return date(value.year, value.month, 1)


def month_end_of(month: date) -> date:
    """The last day of ``month``."""
    following = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    return following - timedelta(days=1)


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)


@dataclass(frozen=True)
class _Computed:
    status: str
    cutoff: date
    measured_on: date
    month_end_valid: bool
    columns: dict
    payload_json: str
    sha256: str


def _plants() -> list[str]:
    configured = {p.strip() for p in get_settings().i7_plants.split(",")}
    return [code for code in ("1300", "1500") if code in configured]


def compute(session: Session, month: date, today: date) -> _Computed:
    """The month's figures, as they stand on ``today``. Reads only; writes nothing."""
    month = first_of_month(month)
    end = month_end_of(month)
    exclusive_end = exclusive_end_of(end)
    cutoff = reporting_cutoff(end, today)
    ended = today > end

    # The recommendations in force at the end of the month: the newest of each material-plant generated up to it.
    position = kpi_definitions.position_figures(session, None, exclusive_end, cutoff)
    by_plant = [{"plant": None, "figures": position.model_dump(mode="json")}] + [
        {"plant": code, "figures": kpi_definitions.position_figures(session, None, exclusive_end, cutoff, code).model_dump(mode="json")}
        for code in _plants()
    ]
    levels = kpi_definitions.stock_levels(session, None, exclusive_end)

    payload = {
        "calc_version": CALC_VERSION,
        "plant_positions": by_plant,
        "stock_levels": [{**level, "current": str(level["current"]) if level["current"] is not None else None,
                          "recommended": str(level["recommended"]) if level["recommended"] is not None else None} for level in levels],
    }
    stockout = position.stockout
    columns = dict(
        latest_consumption_date=_latest_issue_date(session),
        stockout_usage_as_of=stockout.as_of,
        stock_loaded_at=_stock_loaded_at(session),
        population=position.population,
        stockout_at_risk=stockout.at_risk,
        stockout_evaluated=stockout.evaluated,
        stockout_insufficient=stockout.insufficient_data,
        # "Stale" also covers a usage that was never built: there is nothing to trust either way.
        stockout_stale=stockout.stale or stockout.as_of is None,
        excess_count=position.excess_count,
        excess_opportunity=position.excess_opportunity,
        net_rop_value_impact=position.net_rop_value_impact,
        priced_count=position.priced_count,
        currency=position.currency,
        calc_version=CALC_VERSION,
    )
    # A month-end position needs the month to have ended and the measurement to be close to its end. An open month's
    # rolling reading is never a month-end figure.
    age = (today - end).days
    month_end_valid = ended and 0 <= age <= MAX_MEASUREMENT_AGE_DAYS

    fingerprint_source = {"columns": {k: v for k, v in columns.items() if k not in ("stock_loaded_at", "latest_consumption_date")}, "payload": payload}
    return _Computed(
        status=STATUS_FINAL if ended else STATUS_OPEN,
        cutoff=cutoff,
        measured_on=today,
        month_end_valid=month_end_valid,
        columns=columns,
        payload_json=_canonical(payload),
        sha256=hashlib.sha256(_canonical(fingerprint_source).encode("utf-8")).hexdigest(),
    )


def _log(session: Session, month: date, action: str, *, revision=None, previous=None, current=None, detail=None) -> None:
    session.add(
        KpiSnapshotLog(
            month=month, action=action, revision=revision, previous_sha256=previous, payload_sha256=current,
            detail=(detail or "")[:1000] or None,
        )
    )


def capture_month(session: Session, month: date, *, today: date | None = None, force: bool = False) -> CaptureResult:
    """Capture (or refresh) one month's snapshot. Commits on success; on failure logs it and raises."""
    today = today or datetime.now(timezone.utc).date()
    month = first_of_month(month)
    if month > today:
        raise ValueError(f"{month:%Y-%m} has not started: a month cannot be captured before it begins.")

    try:
        computed = compute(session, month, today)
        existing = session.execute(select(KpiSnapshot).where(KpiSnapshot.month == month)).scalar_one_or_none()
        result = _write(session, month, computed, existing, force)
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        # Its own transaction: the failure must be on record even though the capture was rolled back.
        _log(session, month, FAILED, detail=f"{type(exc).__name__}: {exc}")
        session.commit()
        logger.exception("i7.snapshots.capture.failed", extra={"month": f"{month:%Y-%m}"})
        raise


def _write(session: Session, month: date, computed: _Computed, existing: KpiSnapshot | None, force: bool) -> CaptureResult:
    now = datetime.now(timezone.utc)
    fields = dict(
        status=computed.status, cutoff=computed.cutoff, measured_on=computed.measured_on,
        month_end_valid=computed.month_end_valid, payload_json=computed.payload_json,
        payload_sha256=computed.sha256, **computed.columns,
    )

    if existing is None:
        row = KpiSnapshot(month=month, revision=1, created_at=now, updated_at=now, **fields)
        session.add(row)
        _log(session, month, CREATED, revision=1, current=computed.sha256, detail=f"{computed.status}, measured {computed.measured_on}")
        return CaptureResult(month, CREATED, row)

    if existing.status == STATUS_FINAL:
        if not force:
            _log(session, month, REFUSED, revision=existing.revision, previous=existing.payload_sha256, current=computed.sha256,
                 detail="the snapshot is final; capture again with force to replace it")
            return CaptureResult(month, REFUSED, existing, "the snapshot is final; it was not replaced")
        revision = existing.revision + 1
        previous = existing.payload_sha256
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.revision, existing.updated_at = revision, now
        _log(session, month, REPLACED_FINAL, revision=revision, previous=previous, current=computed.sha256,
             detail=f"a final snapshot was replaced on request; measured {computed.measured_on}")
        return CaptureResult(month, REPLACED_FINAL, existing)

    # OPEN: unchanged figures change nothing, unless the month has now ended (then it is finalised).
    finalising = computed.status == STATUS_FINAL
    if existing.payload_sha256 == computed.sha256 and not finalising:
        return CaptureResult(month, UNCHANGED, existing)
    revision = existing.revision + 1
    previous = existing.payload_sha256
    for key, value in fields.items():
        setattr(existing, key, value)
    existing.revision, existing.updated_at = revision, now
    action = FINALISED if finalising else UPDATED
    _log(session, month, action, revision=revision, previous=previous, current=computed.sha256,
         detail=f"{computed.status}, measured {computed.measured_on}")
    return CaptureResult(month, action, existing)


def capture_due(session: Session, today: date | None = None) -> list[CaptureResult]:
    """What the nightly job asks for: refresh the current month's rolling reading, and finalise the previous month if
    it has no final snapshot yet. A month already final, or already read today, is left alone without recomputing it."""
    today = today or datetime.now(timezone.utc).date()
    current = first_of_month(today)
    previous = date(current.year - 1, 12, 1) if current.month == 1 else date(current.year, current.month - 1, 1)
    results: list[CaptureResult] = []
    for month in (previous, current):
        existing = session.execute(select(KpiSnapshot).where(KpiSnapshot.month == month)).scalar_one_or_none()
        if existing is not None and (existing.status == STATUS_FINAL or existing.measured_on == today):
            continue
        results.append(capture_month(session, month, today=today))
    return results


def load_for_months(session: Session, months: list[date]) -> dict[date, KpiSnapshot]:
    if not months:
        return {}
    rows = session.execute(select(KpiSnapshot).where(KpiSnapshot.month.in_(months))).scalars().all()
    return {row.month: row for row in rows}
