"""Open documents, re-read by key several times a day.

WHY

The hourly delta reads each set by one date (``manifest.DELTAS``). For several
sets that date is a CREATION date -- a requisition's Badat, a reservation's
Crdat, a purchase order's Aedat -- so an edit to an existing row never moves
it, and the hourly window never sees the row again. Purchase order schedule
lines have no date of their own at all; they ride on their item's change date,
which a delivery-date change on the line alone may not move. The nightly
refresh (``app.ingest.refresh``) re-reads the last 12 months once a day, so
such an edit reaches the pages up to a day late -- and an open document older
than the window not at all, except requisitions and reservations, which the
refresh re-reads by key.

The rows that matter most are the ones still OPEN: a requisition not yet a
purchase order, a reservation not yet issued, a purchase order still waiting
for goods. They are few -- about 1,700 open requisitions, 10,000 open
reservation items and 1,100 open purchase orders on PRD (24 Nov 2026) -- so
they can be re-read by key several times a day, whatever their age.

WHAT

For each set in ``OPEN_DOCS_SETS``, the document keys that are open in our
table now (``OPEN_DOCS``), read from SAP by key (``fetch_set(...,
keys_only=True)``) and merged by the load every delta uses (``load_set``):
changed rows updated, new rows inserted, nothing deleted, no watermark moved.
Under the delta's lock, at the hours ``OPEN_DOCS_HOURS_UTC`` names
(``scheduler._open_docs_tick``).

It also compares, for every document it read, the rows our table holds with
the rows SAP returned. A row we hold that SAP no longer returns for its own
document was deleted in SAP -- a schedule line replaced, a reservation item
removed -- and the merge, which never deletes, would keep it forever. Those
are LOGGED and reported, not deleted: what to do with them is a decision for
the pages, made once the numbers are known.

By hand::

    python -m app.ingest --open-docs                          # the configured list
    python -m app.ingest --open-docs --set POScheduleLineSet  # one set
"""

from __future__ import annotations

import calendar
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from app.core.config import get_settings
from app.core.logging import get_logger
from app.ingest.manifest import IngestSpec, spec_for

logger = get_logger(__name__)


# --- What "open" means, per set ------------------------------------------------


def _months_back(today: date, months: int) -> date:
    """The same calendar day ``months`` months before ``today``, clamped to the
    month's length (refresh.Window uses the same arithmetic)."""
    total = today.year * 12 + (today.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _open_requisitions_sql() -> str:
    """Requisitions not deleted, not closed and not yet on a purchase order --
    the refresh's own rule (``refresh.OPEN_ROWS``), with no age limit."""
    from app.ingest.refresh import OPEN_ROWS

    rule = OPEN_ROWS["PurchaseRequisitionSet"]
    return (
        "SELECT DISTINCT [BANFN] FROM raw_eban "
        f"WHERE ({rule.predicate}) AND ISNULL(LTRIM([BANFN]), '') <> ''"
    )


def _open_reservations_sql() -> str:
    """Reservations with an item neither deleted nor final-issued -- the
    refresh's own rule, with no age limit."""
    from app.ingest.refresh import OPEN_ROWS

    rule = OPEN_ROWS["ReservationItemSet"]
    return (
        "SELECT DISTINCT [RSNUM] FROM raw_resb "
        f"WHERE ({rule.predicate}) AND ISNULL(LTRIM([RSNUM]), '') <> ''"
    )


def _open_purchase_orders_sql() -> str:
    """Purchase orders with a schedule line not fully received, on an item
    neither deleted nor delivery-completed, due on or after ``:start``.

    Read through the ``n_eket`` / ``n_ekpo`` views, so quantities and dates are
    parsed exactly as the pages parse them (CSV notation, OData notation, SAP's
    '00000000'), and keys compare without their padding. The due-date bound is
    what keeps this small: by the flags alone 49,331 purchase orders on PRD
    have an open item, most from 2013-2016 and never closed in SAP; 1,117 have
    one due in the last 12 months (24 Nov 2026).
    """
    return (
        "SELECT DISTINCT k.purchasing_document FROM n_eket k "
        "JOIN n_ekpo p ON p.purchasing_document = k.purchasing_document AND p.item = k.item "
        "WHERE ISNULL(LTRIM(p.deletion_indicator), '') = '' "
        "AND ISNULL(LTRIM(p.delivery_completed), '') = '' "
        "AND TRY_CAST(k.scheduled_quantity AS decimal(38, 6)) "
        "    > COALESCE(TRY_CAST(k.qty_delivered AS decimal(38, 6)), 0) "
        "AND TRY_CAST(k.delivery_date AS date) >= :start "
        "AND ISNULL(LTRIM(k.purchasing_document), '') <> ''"
    )


@dataclass(frozen=True)
class OpenDocsRule:
    """How to find a set's open documents: the OData property they are read
    back by (``key``, measured HONOURED as a filter on PRD), and the SQL that
    lists the open values of it. Sets sharing ``group`` share one key list."""

    key: str
    sql: Callable[[], str]
    group: str
    needs_start: bool = False


OPEN_DOCS: dict[str, OpenDocsRule] = {
    "PurchaseRequisitionSet": OpenDocsRule("Banfn", _open_requisitions_sql, "requisitions"),
    "ReservationItemSet": OpenDocsRule("Rsnum", _open_reservations_sql, "reservations"),
    # A purchase order is one document: its header, items and schedule lines
    # are re-read together, by the same order numbers.
    "PurchaseOrderSet": OpenDocsRule("Ebeln", _open_purchase_orders_sql, "purchase_orders", needs_start=True),
    "PurchaseOrderItemSet": OpenDocsRule("Ebeln", _open_purchase_orders_sql, "purchase_orders", needs_start=True),
    "POScheduleLineSet": OpenDocsRule("Ebeln", _open_purchase_orders_sql, "purchase_orders", needs_start=True),
}


def open_keys(spec: IngestSpec, rule: OpenDocsRule, *, start: date, engine=None) -> list[str]:
    """This set's open document keys, padded the way OData spells them,
    distinct and sorted. Raises on a database error, or when there are more
    than the re-read is sized for (``refresh.OPEN_ROWS_KEY_LIMIT``)."""
    from sqlalchemy import text

    from app.core.db import get_engine
    from app.ingest.refresh import OPEN_ROWS_KEY_LIMIT, pad_key

    engine = engine or get_engine()
    params = {"start": start} if rule.needs_start else {}
    with engine.connect() as connection:
        rows = connection.execute(text(rule.sql()), params).fetchall()
    # An all-zero key is SAP's "none" ('0000000000': no reservation).
    keys = sorted({
        key for key in (pad_key(r[0], spec, rule.key) for r in rows if r[0] is not None)
        if str(key).strip("0").strip()
    })
    if len(keys) > OPEN_ROWS_KEY_LIMIT:
        raise RuntimeError(
            f"{len(keys):,} open {rule.key} value(s), above the {OPEN_ROWS_KEY_LIMIT:,} "
            "this re-read is sized for"
        )
    return keys


# --- The cycle ------------------------------------------------------------------


@dataclass
class OpenDocsOutcome:
    """One set's run, for the log, the CLI table and the summary."""

    name: str
    keys: int = 0
    rows: int = 0
    requests: int = 0
    seconds: float = 0.0
    raw: str | None = None
    missing_in_sap: int = 0
    missing_sample: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _check_missing(spec: IngestSpec, rule: OpenDocsRule, keys: list[str], prefix: str, outcome: OpenDocsOutcome) -> None:
    """Rows our table holds for these documents that SAP did not return.
    Logged and counted; never deleted. Never raises."""
    from app.core.db import get_engine
    from app.core.storage import get_storage
    from app.ingest import sync_rows

    try:
        engine = get_engine()
        shape = sync_rows.raw_shape(spec, engine)
        key_column = shape.column_for(rule.key) if shape else None
        if shape is None or key_column is None:
            return
        sap = sync_rows.landed_rows(get_storage(), prefix)
        ours = sync_rows.db_rows_for_keys(
            engine, shape, key_column, sync_rows.key_variants(keys, spec, rule.key), shape.identity_columns
        )
        missing = sync_rows.missing_in_sap(sap, ours, shape)
    except Exception as exc:
        logger.warning("%s: could not compare our rows with SAP's -- %s", spec.name, exc)
        return
    outcome.missing_in_sap = len(missing)
    outcome.missing_sample = ["/".join(m) for m in missing[: sync_rows.SAMPLE_LIMIT]]
    if missing:
        logger.warning(
            "%s: %d row(s) in %s that SAP no longer returns for their document "
            "(deleted in SAP?; kept, not deleted) -- e.g. %s",
            spec.name, len(missing), shape.table, ", ".join(outcome.missing_sample),
        )


def _run_one(name: str, root: str, today: date, keys_by_group: dict[str, list[str]], outcome: OpenDocsOutcome) -> None:
    """Fetch and merge one set's open documents. Raises with the reason on any failure."""
    from app.ingest.fetch import fetch_set
    from app.ingest.load import STATUS_SUCCEEDED, load_set

    rule = OPEN_DOCS.get(name)
    if rule is None:
        raise RuntimeError(f"no rule for what an open {name} document is (open_docs.OPEN_DOCS)")
    spec = spec_for(name)
    if spec.blocked:
        raise RuntimeError(f"cannot be read: {spec.blocked}")

    if rule.group not in keys_by_group:
        start = _months_back(today, max(get_settings().open_docs_due_months, 1))
        keys_by_group[rule.group] = open_keys(spec, rule, start=start)
    keys = keys_by_group[rule.group]
    outcome.keys = len(keys)

    fetched = fetch_set(
        spec, root=root, keys_only=True, extra_keys=(rule.key, keys), advance_watermark=False,
    )
    outcome.rows = fetched.rows
    outcome.requests = fetched.requests
    if not fetched.ok:
        raise RuntimeError(fetched.error or "fetch unstable")
    if not keys:
        outcome.raw = "nothing open"
        return

    loaded = load_set(spec, root=root, prefix=fetched.prefix)
    if loaded.status != STATUS_SUCCEEDED:
        raise RuntimeError(loaded.error or loaded.status)
    outcome.raw = loaded.raw or "loaded"
    _check_missing(spec, rule, keys, fetched.prefix, outcome)


def run_open_docs_cycle(names: list[str] | None = None, *, today: date | None = None) -> dict:
    """Every set in ``OPEN_DOCS_SETS`` (or ``names``): open documents read by
    key and merged. Never raises.

    Synchronous and blocking, like the other cycles; the scheduler runs it in a
    worker thread. The same SAP-system check as the delta. One set's failure
    costs that set.
    """
    from app.ingest import sap_system

    settings = get_settings()
    chosen = list(names) if names is not None else settings.open_docs_set_list
    today = today or datetime.now(timezone.utc).date()
    # Landed apart from the delta's and the refresh's files, so the three never
    # overwrite each other's evidence for the day.
    root = f"{settings.ingest_prefix.strip('/')}/open_docs"
    summary: dict = {"pulled": 0, "failed": 0, "errors": [], "sets": [], "missing_in_sap": 0}

    try:
        mismatch = sap_system.verdict(sap_system.recorded(), sap_system.configured())
    except Exception as exc:
        mismatch = f"could not check which SAP system the database holds: {exc}"
    if mismatch:
        summary["errors"] = [f"open documents re-read refused: {mismatch}"]
        summary["failed"] = len(chosen)
        logger.error("open documents re-read refused: %s", mismatch)
        return summary

    keys_by_group: dict[str, list[str]] = {}
    for name in chosen:
        outcome = OpenDocsOutcome(name=name)
        started = time.monotonic()
        try:
            _run_one(name, root, today, keys_by_group, outcome)
        except Exception as exc:
            outcome.error = str(exc) or type(exc).__name__
        outcome.seconds = time.monotonic() - started
        summary["sets"].append(asdict(outcome))
        summary["missing_in_sap"] += outcome.missing_in_sap
        if outcome.ok:
            summary["pulled"] += 1
            logger.info(
                "%s: open documents re-read (%d key(s), %d row(s), %d request(s), %.0fs) -- %s",
                outcome.name, outcome.keys, outcome.rows, outcome.requests, outcome.seconds, outcome.raw,
            )
        else:
            summary["failed"] += 1
            summary["errors"].append(f"{outcome.name}: {outcome.error}")
            logger.error("%s: open documents re-read failed -- %s", outcome.name, outcome.error)
    return summary
