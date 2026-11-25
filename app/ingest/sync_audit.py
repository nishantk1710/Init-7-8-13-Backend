"""The sync audit: how far ``raw_<table>`` has drifted from SAP, measured.

WHY

Every route that keeps a raw table current -- the hourly delta, its
look-backs, the re-reads through parents' keys, the nightly refresh, the
open-documents re-read -- was added because a gap was found by hand, usually
by a user noticing a wrong number. Nothing measured how many rows are stale
behind the watermarks, which tables drift, or by which fields. This does.

WHAT

Once a day, for each set in ``SYNC_AUDIT_SETS``: a random sample of document
keys from our table (``SYNC_AUDIT_SAMPLE``; for a set with an open-documents
rule, half of them open documents, where drift matters most), read from SAP by
key, and held against our rows field by field:

* ``differ`` -- a matched row with a field whose VALUE differs (a quantity, a
  date, a flag), as the ``n_<table>`` views would read it;
* ``empty_here`` -- a matched row whose only real differences are fields empty
  in our table that SAP fills. Mostly columns the CSV extract left blank, which
  the merge fills the next time it reads the row; but a flag SAP set later (a
  deletion flag) lands here too, so the fields are listed;
* ``notation`` -- a matched row that differs only in how a number or date is
  written (a CSV-loaded '1.234,5' against OData's '1234.5'): invisible to the
  pages, though the merge would rewrite it. In a column the views do not read
  as a number (EBAN PREIS, BSMNG), the CSV loaders never converted the
  extract's notation, so any notation that gives SAP's number counts;
* ``missing_here`` -- a row SAP returns for a sampled document that our table
  lacks;
* ``missing_in_sap`` -- a row we hold for a sampled document that SAP no
  longer returns (deleted in SAP; the merge never deletes).

Logged per set and summarised; queryable in Log Analytics. Read-only, unless
``SYNC_AUDIT_REPAIR`` is on: then the sampled rows are also merged by the load
every delta uses. Only the fields the OData set carries can be compared; a
column only the CSV extract delivered is out of reach.

By hand::

    python -m app.ingest --sync-audit                       # the configured list
    python -m app.ingest --sync-audit --set MaterialPlantSet
"""

from __future__ import annotations

import random
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.ingest.manifest import IngestSpec, spec_for

logger = get_logger(__name__)


# The property each set's documents are sampled and read back by: the leading
# column of the table's key index, and measured HONOURED as a $filter on PRD
# (data-generator/discovery_qa/filter_support.csv).
AUDIT_RULES: dict[str, str] = {
    "PurchaseRequisitionSet": "Banfn",
    "ReservationItemSet": "Rsnum",
    "PurchaseOrderSet": "Ebeln",
    "PurchaseOrderItemSet": "Ebeln",
    "POScheduleLineSet": "Ebeln",
    "MaterialSet": "Matnr",
    "MaterialPlantSet": "Matnr",
    "MaterialDescriptionSet": "Matnr",
    "StorageLocationStockSet": "Matnr",
    "BatchStockSet": "Matnr",
    "MaterialValuationSet": "Matnr",
    "VendorSet": "Lifnr",
    "InfoRecordSet": "Infnr",
    "InfoRecordOrgSet": "Infnr",
}

# Field examples kept per set for the log line.
EXAMPLES = 3


# --- Comparing one value the way the pages read it ------------------------------

_DMY = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_DATS = re.compile(r"^\d{8}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _as_view_date(text: str) -> str | None:
    """The ``n_<table>`` date rule (sap_normalise._Dialect.date): ISO kept,
    DD.MM.YYYY and YYYYMMDD turned to ISO, SAP's zero dates NULL. ``None``
    when the text is not a date at all."""
    if text in ("00000000", "00.00.0000"):
        return ""
    if _ISO.match(text):
        return text
    if _DMY.match(text):
        return f"{text[6:10]}-{text[3:5]}-{text[0:2]}"
    if _DATS.match(text) and text[:2] in ("19", "20"):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return None


def _as_number(text: str, fmt: str | None) -> Decimal | None:
    """A number as written: in the CSV extract's notation ``fmt`` (German
    '1.234,5', US '2,490.00') when given, plain ('1234.5', OData's) when not;
    a trailing minus moved to the front, as the views do."""
    t = text
    if fmt == "german":
        t = t.replace(".", "").replace(",", ".")
    elif fmt == "us":
        t = t.replace(",", "")
    if t.endswith("-"):
        t = "-" + t[:-1]
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


# A number as either route writes one: digits with dots and commas, and SAP's
# trailing minus. Dates are told apart before this is asked.
_NUMBER = re.compile(r"^-?\d[\d.,]*-?$")


def _same_number(a: str, b: str) -> bool:
    """Whether ours (``a``) is SAP's number (``b``) in some notation, for a
    column the views do not read as a number.

    The CSV loaders convert only the columns the views read as numbers
    (CSV_LOAD_NUMBER_FORMAT); every other column keeps the extract's own
    notation -- on PRD German: '12.640,68' where OData sends '12640.68' (EBAN
    PREIS and BSMNG, measured 25 Nov 2026). No view says which notation such a
    column holds, so any of the three that gives SAP's number counts. A blank
    side is a zero, as for a quantity.
    """
    if (a and not _NUMBER.match(a)) or (b and not _NUMBER.match(b)):
        return False
    theirs = _as_number(b or "0", None)
    return theirs is not None and theirs in {_as_number(a or "0", fmt) for fmt in (None, "german", "us")}


def compare(ours: Any, theirs: Any, *, numeric: bool, number_format: str) -> str:
    """``same``, ``notation``, ``empty_here`` or ``value``: how our stored value
    stands against SAP's (``theirs``, shaped as the merge would write it).

    A number is compared as a number. In a column the views read as one: ours
    in the CSV extract's notation (``number_format``) when it carries a comma
    -- a CSV-loaded value never touched by OData -- and plain otherwise, as the
    views read it. In any other column: in whichever notation matches
    (``_same_number``). SAP's is always plain. A date is compared as a day,
    whichever of SAP's spellings each side holds. A real difference where ours
    is empty is ``empty_here``, kept apart from a changed value.
    """
    a = "" if ours is None else str(ours).strip()
    b = "" if theirs is None else str(theirs).strip()
    if a == b:
        return "same"
    verdict = _differs_by(a, b, numeric=numeric, number_format=number_format)
    return "empty_here" if verdict == "value" and not a else verdict


def _differs_by(a: str, b: str, *, numeric: bool, number_format: str) -> str:
    """``notation`` or ``value``, for two stripped texts that differ."""
    if numeric:
        na = _as_number(a or "0", number_format if "," in a else None)
        nb = _as_number(b or "0", None)
        if na is not None and nb is not None:
            return "notation" if na == nb else "value"
    da, db = _as_view_date(a) if a else "", _as_view_date(b) if b else ""
    if da is not None and db is not None and (a or b):
        return "notation" if da == db else "value"
    if a.isdigit() and b.isdigit():
        return "notation" if int(a) == int(b) else "value"
    # A flag: the merge writes SAP's false as '' (raw_merge.shape).
    if {a.lower(), b.lower()} <= {"", "false"}:
        return "notation"
    if not numeric and _same_number(a, b):
        return "notation"
    return "value"


# --- The sample ---------------------------------------------------------------------


def sample_keys(spec: IngestSpec, key_prop: str, key_column: str, table: str, n: int, *, engine=None,
                today: date | None = None, rng: random.Random | None = None) -> list[str]:
    """``n`` document keys, OData-padded: half open documents where the set has
    an open-documents rule, the rest at random from the whole table."""
    from sqlalchemy import text

    from app.core.db import get_engine
    from app.ingest import open_docs
    from app.ingest.refresh import pad_key
    from app.seed.sqlserver import quote

    engine = engine or get_engine()
    rng = rng or random.Random()
    chosen: dict[str, None] = {}

    rule = open_docs.OPEN_DOCS.get(spec.name)
    if rule is not None and rule.key == key_prop:
        try:
            start = open_docs._months_back(today or datetime.now(timezone.utc).date(),
                                           max(get_settings().open_docs_due_months, 1))
            opened = open_docs.open_keys(spec, rule, start=start, engine=engine)
            for key in rng.sample(opened, min(len(opened), n // 2)):
                chosen.setdefault(key, None)
        except Exception as exc:
            logger.warning("%s: open documents not sampled -- %s", spec.name, exc)

    want = max(n - len(chosen), 0)
    if want:
        column = quote(key_column)
        sql = (
            f"SELECT TOP ({int(want) * 2}) k FROM (SELECT DISTINCT {column} AS k FROM {quote(table)} "
            f"WHERE ISNULL(LTRIM({column}), '') <> '') d ORDER BY NEWID()"
        )
        with engine.connect() as connection:
            for (value,) in connection.execute(text(sql)):
                key = pad_key(value, spec, key_prop)
                if str(key).strip("0").strip():
                    chosen.setdefault(key, None)
                if len(chosen) >= n:
                    break
    return list(chosen)[:n]


# --- One set ------------------------------------------------------------------------


@dataclass
class AuditOutcome:
    """One set's audit, for the log, the CLI table and the summary."""

    name: str
    documents: int = 0
    sap_rows: int = 0
    our_rows: int = 0
    same: int = 0
    differ: int = 0
    notation: int = 0
    empty_here: int = 0
    missing_here: int = 0
    missing_in_sap: int = 0
    fields: dict[str, int] = field(default_factory=dict)
    empty_fields: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
    repaired: str | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def drift_rate(self) -> float:
        """Changed values only; rows that are just empty here are counted apart."""
        matched = self.same + self.differ + self.notation + self.empty_here
        return (self.differ / matched) if matched else 0.0


def audit_rows(spec: IngestSpec, shape, sap_rows: list[dict], our_rows: list[dict], outcome: AuditOutcome,
               *, number_format: str) -> None:
    """Fill ``outcome`` from SAP's rows and ours for the same documents."""
    from app.ingest import sync_rows
    from app.ingest.raw_merge import MATERIAL_NUMBER_PROPERTIES, VOCAB_SAP, external_material_number
    from app.ingest.raw_merge import shape as shape_value

    ours_by_id: dict[tuple[str, ...], dict] = {}
    for row in our_rows:
        ours_by_id.setdefault(sync_rows.identity_of_db_row(row, shape), row)
    compared = [p for p in shape.columns if p not in shape.identity_props]
    fields: Counter = Counter()
    empty_fields: Counter = Counter()
    returned: set[tuple[str, ...]] = set()

    for sap in sap_rows:
        identity = sync_rows.identity_of_sap_row(sap, shape)
        returned.add(identity)
        ours = ours_by_id.get(identity)
        if ours is None:
            outcome.missing_here += 1
            continue
        verdicts: list[str] = []
        for prop in compared:
            column = shape.columns[prop]
            theirs = shape_value(sap.get(prop), shape.edm.get(prop, "Edm.String"), shape.vocabulary or VOCAB_SAP,
                                 numeric=shape.numeric.get(prop, False))
            if prop in MATERIAL_NUMBER_PROPERTIES:
                theirs = external_material_number(theirs)
            verdict = compare(ours.get(column), theirs, numeric=shape.numeric.get(prop, False),
                              number_format=number_format)
            verdicts.append(verdict)
            if verdict == "value":
                fields[prop] += 1
                if len(outcome.examples) < EXAMPLES:
                    outcome.examples.append(
                        f"{'/'.join(identity)} {prop}: ours={ours.get(column)!r} sap={theirs!r}"
                    )
            elif verdict == "empty_here":
                empty_fields[prop] += 1
        if "value" in verdicts:
            outcome.differ += 1
        elif "empty_here" in verdicts:
            outcome.empty_here += 1
        elif "notation" in verdicts:
            outcome.notation += 1
        else:
            outcome.same += 1

    outcome.missing_in_sap = sum(1 for identity in ours_by_id if identity not in returned)
    outcome.fields = dict(fields.most_common())
    outcome.empty_fields = dict(empty_fields.most_common())


def _audit_one(name: str, root: str, today: date, outcome: AuditOutcome, *, sample: int, repair: bool) -> None:
    """Sample, read, compare -- and merge, when asked. Raises with the reason on any failure."""
    from app.core.db import get_engine
    from app.core.storage import get_storage
    from app.ingest import sync_rows
    from app.ingest.fetch import fetch_set
    from app.ingest.load import STATUS_SUCCEEDED, load_set

    key_prop = AUDIT_RULES.get(name)
    if key_prop is None:
        raise RuntimeError(f"no rule for how to sample {name} (sync_audit.AUDIT_RULES)")
    spec = spec_for(name)
    if spec.blocked:
        raise RuntimeError(f"cannot be read: {spec.blocked}")
    engine = get_engine()
    shape = sync_rows.raw_shape(spec, engine)
    if shape is None:
        raise RuntimeError("no raw table to audit (or its key columns are missing)")
    key_column = shape.column_for(key_prop)
    if key_column is None:
        raise RuntimeError(f"{shape.table} has no column for {key_prop}")

    keys = sample_keys(spec, key_prop, key_column, shape.table, sample, engine=engine, today=today)
    outcome.documents = len(keys)
    fetched = fetch_set(spec, root=root, keys_only=True, extra_keys=(key_prop, keys), advance_watermark=False)
    if not fetched.ok:
        raise RuntimeError(fetched.error or "fetch unstable")
    sap_rows = sync_rows.landed_rows(get_storage(), fetched.prefix) if keys else []
    columns = list(dict.fromkeys([*shape.identity_columns, *shape.columns.values()]))
    our_rows = (
        sync_rows.db_rows_for_keys(engine, shape, key_column, sync_rows.key_variants(keys, spec, key_prop), columns)
        if keys else []
    )
    outcome.sap_rows, outcome.our_rows = len(sap_rows), len(our_rows)
    audit_rows(spec, shape, sap_rows, our_rows, outcome, number_format=get_settings().csv_number_format)

    if repair and keys:
        loaded = load_set(spec, root=root, prefix=fetched.prefix)
        if loaded.status != STATUS_SUCCEEDED:
            raise RuntimeError(f"audited, but the repair merge failed: {loaded.error or loaded.status}")
        outcome.repaired = loaded.raw or "loaded"


def run_sync_audit_cycle(names: list[str] | None = None, *, today: date | None = None,
                         sample: int | None = None, repair: bool | None = None) -> dict:
    """Every set in ``SYNC_AUDIT_SETS`` (or ``names``), audited. Never raises."""
    from app.ingest import sap_system

    settings = get_settings()
    chosen = list(names) if names is not None else settings.sync_audit_set_list
    today = today or datetime.now(timezone.utc).date()
    sample = max(int(sample if sample is not None else settings.sync_audit_sample), 1)
    repair = settings.sync_audit_repair if repair is None else repair
    root = f"{settings.ingest_prefix.strip('/')}/sync_audit"
    summary: dict = {"audited": 0, "failed": 0, "errors": [], "sets": []}

    try:
        mismatch = sap_system.verdict(sap_system.recorded(), sap_system.configured())
    except Exception as exc:
        mismatch = f"could not check which SAP system the database holds: {exc}"
    if mismatch:
        summary["errors"] = [f"sync audit refused: {mismatch}"]
        summary["failed"] = len(chosen)
        logger.error("sync audit refused: %s", mismatch)
        return summary

    for name in chosen:
        outcome = AuditOutcome(name=name)
        started = time.monotonic()
        try:
            _audit_one(name, root, today, outcome, sample=sample, repair=repair)
        except Exception as exc:
            outcome.error = str(exc) or type(exc).__name__
        outcome.seconds = time.monotonic() - started
        summary["sets"].append({**asdict(outcome), "drift_rate": round(outcome.drift_rate, 4)})
        if outcome.ok:
            summary["audited"] += 1
            top = ", ".join(f"{k}x{v}" for k, v in list(outcome.fields.items())[:6]) or "none"
            empty = ", ".join(f"{k}x{v}" for k, v in list(outcome.empty_fields.items())[:6])
            log = logger.warning if (outcome.differ or outcome.empty_here or outcome.missing_here
                                     or outcome.missing_in_sap) else logger.info
            log(
                "%s: sync audit -- %d document(s), %d SAP row(s), %d ours: %d same, %d differ (%.1f%%), "
                "%d notation only, %d empty here but set in SAP, %d missing here, %d missing in SAP; "
                "fields: %s%s%s%s (%.0fs)",
                outcome.name, outcome.documents, outcome.sap_rows, outcome.our_rows, outcome.same,
                outcome.differ, outcome.drift_rate * 100, outcome.notation, outcome.empty_here,
                outcome.missing_here, outcome.missing_in_sap, top,
                f"; empty here: {empty}" if empty else "",
                f"; e.g. {' | '.join(outcome.examples)}" if outcome.examples else "",
                f"; repaired: {outcome.repaired}" if outcome.repaired else "",
                outcome.seconds,
            )
        else:
            summary["failed"] += 1
            summary["errors"].append(f"{outcome.name}: {outcome.error}")
            logger.error("%s: sync audit failed -- %s", outcome.name, outcome.error)
    return summary
