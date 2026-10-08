#!/usr/bin/env python3
"""Live CSV check: fire SAP's CSV extract, then prove what landed.

STANDALONE. Imports nothing from ``app/``. It shares the field catalogue and
the CPI client with its sibling ``odata_live_check.py`` (same folder, keep the
two together) so both checks measure the same list of fields.

The route under test (Interface 2):

    this script --GET TableExtractSet(...)--> CPI --> SAP
    SAP background job --POST chunks of 50,000 rows--> the app's /api/events/csv
    the app --> ADLS  csv/<TABLE>/unattributed-<YYYY-MM-DD>/<TABLE>.csv

A request fired from here has no row in csv_extract_request, so the app lands
it as "unattributed" and nothing loads it into SQL -- which is exactly what a
test wants: the data is inspected, nothing downstream changes. This script
READS storage and never writes it.

What it checks, per table:

  1. The fire   -- RequestId in the shape SAP delivers on (F + 4 letters + 8
                   digits, 13 chars), and SAP's "Success: ... started" ack.
  2. Delivery   -- new bytes appear in ADLS; done after --quiet seconds with no
                   growth (SAP never marks a last chunk); timeout if nothing
                   arrives within --first-chunk-timeout (5 min: every table that
                   delivers has started within 90 s so far). Only the bytes added
                   after the fire are analysed, so earlier runs the same day do
                   not pollute the result.
  3. Header     -- every field the initiatives need is present; blank column
                   names (the EKPO defect) and duplicate names are listed.
  4. The data   -- rows, ragged rows, duplicate keys, fill rate and non-zero
                   rate per required field, date ranges, plant spread.
  5. Reconcile  -- CSV rows against OData $count for the same scope (MAKT
                   English only, CDHDR five object classes), plus a spot check
                   that the first OData keys are all present in the CSV.
  6. Coverage   -- with --odata-report: per initiative, which required fields
                   come from OData, from CSV, from both, or from neither.

Usage -- on the Azure SSH box, from backend/:

    python live_checks/csv_live_check.py                         # all 21, two at a time, analyse
    python live_checks/csv_live_check.py --batch-size 0          # all 21 fired at once
    python live_checks/csv_live_check.py --tables EKPO MSEG
    python live_checks/csv_live_check.py --inspect               # no fire: analyse latest landed files
    python live_checks/csv_live_check.py --odata-report /home/live_checks/odata_<time>.json

Two tables are fired, watched until both have delivered (2 minutes with no
growth) or timed out, then the next two -- so each batch costs its delivery
plus 2 minutes. --batch-size 0 fires them all at once. Run it under nohup:

    nohup python -u live_checks/csv_live_check.py > /home/live_checks/csv_run.log 2>&1 &
    tail -f /home/live_checks/csv_run.log

Configuration: the CPI_* settings, STORAGE_URL (abfss://...), optionally
AZURE_STORAGE_ACCOUNT_KEY (else managed identity) and DATABASE_URL (to refuse
firing a table the app already has an extract open for).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from odata_live_check import (  # noqa: E402 - sibling module, same folder
    BY_SAP,
    CATALOGUE,
    DATE_FIELDS,
    DEFAULT_SERVICES,
    INITIATIVES,
    NUMERIC_FIELDS,
    PLANT_FIELDS,
    Cpi,
    Table,
    count,
    default_out,
    fetch_metadata,
    fmt_n,
    load_config,
    norm,
    norm_key,
    odata_property,
    read_page,
    short_error,
)

CSV_SERVICE = "ZMM_GET_CSV_SRV"
CSV_PREFIX = "csv"
REQUEST_ID_DIGITS = 8
FULL_FROM, FULL_TO = "19000101", "20271231"

# --------------------------------------------------------------------------
# ADLS (read-only)
# --------------------------------------------------------------------------


class Lake:
    def __init__(self, config: dict[str, str]) -> None:
        url = config.get("STORAGE_URL") or ""
        parsed = urlparse(url)
        if parsed.scheme not in ("abfs", "abfss") or "@" not in parsed.netloc:
            raise SystemExit(f"STORAGE_URL must be abfss://<container>@<account>.dfs.core.windows.net/<path>, got {url!r}")
        container, _, host = parsed.netloc.partition("@")
        self.base = parsed.path.strip("/")
        try:
            from azure.storage.filedatalake import DataLakeServiceClient
        except ImportError:
            raise SystemExit("azure-storage-file-datalake is not installed (it is in requirements.txt)") from None
        credential: Any = config.get("AZURE_STORAGE_ACCOUNT_KEY") or None
        if credential is None:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        service = DataLakeServiceClient(account_url=f"https://{host}", credential=credential)
        self.fs = service.get_file_system_client(container)

    def _path(self, key: str) -> str:
        return f"{self.base}/{key}" if self.base else key

    def size(self, key: str) -> int:
        try:
            return int(self.fs.get_file_client(self._path(key)).get_file_properties().size)
        except Exception as exc:  # ResourceNotFoundError and friends
            if "NotFound" in type(exc).__name__ or "PathNotFound" in str(exc) or "BlobNotFound" in str(exc):
                return 0
            raise

    def read(self, key: str, offset: int = 0) -> bytes:
        client = self.fs.get_file_client(self._path(key))
        if offset:
            return client.download_file(offset=offset).readall()
        return client.download_file().readall()

    def latest(self, table: str) -> str | None:
        """The most recently written <TABLE>.csv under csv/<TABLE>/, any folder."""
        prefix = self._path(f"{CSV_PREFIX}/{table}")
        best = None
        try:
            for item in self.fs.get_paths(path=prefix, recursive=True):
                if item.is_directory or not item.name.endswith(f"/{table}.csv"):
                    continue
                if best is None or item.last_modified > best.last_modified:
                    best = item
        except Exception:
            return None
        if best is None:
            return None
        name = best.name
        return name[len(self.base) + 1:] if self.base and name.startswith(self.base + "/") else name


# Tables whose 30-Sep-2026 extract shape the app's header matching does not
# recognise yet. It lands them under a fingerprint of the new header instead
# of the table name, so the watcher must look there too. The hash is
# sha256(",".join(upper header))[:8], stable per header; observed live:
#   MARA        MATNR,MTART,MATKL,MEINS,BISMT,LVORM,MSTAE        (narrow, no MANDT)
#   EKKO        EBELN,BSART,BEDAT,AEDAT,LIFNR,EKORG,EKGRP,WAERS  (narrow)
#   EKET        EBELN,EBELP,ETENR,EINDT,MENGE,WEMNG              (narrow)
#   ZMM_GP_*    full wide Z-table headers
# Remove an entry once the app recognises that header and lands it normally.
UNKNOWN_LANDINGS: dict[str, tuple[str, ...]] = {
    "MARA": ("UNKNOWN_54defdac",),
    "EKKO": ("UNKNOWN_d7d70fc7",),
    "EKET": ("UNKNOWN_112cee99",),
    "ZMM_GP_HDR": ("UNKNOWN_01fe0bba",),
    "ZMM_GP_ITEM": ("UNKNOWN_1c6e0cf6",),
    "ZMM_GP_IN": ("UNKNOWN_8e167a4d",),
}


def landing_candidates(table: str) -> list[str]:
    """Where the app puts an unattributed chunk: dated by the server's day.
    Today and tomorrow (UTC) and the local day, so a run across midnight is
    still seen whole. A table the app cannot identify by header lands under
    its fingerprint, so those paths are watched as well."""
    now = datetime.now(timezone.utc)
    days = {now.date(), (now + timedelta(days=1)).date(), date.today()}
    names = (table, *UNKNOWN_LANDINGS.get(table, ()))
    return [f"{CSV_PREFIX}/{name}/unattributed-{d:%Y-%m-%d}/{name}.csv"
            for name in names for d in sorted(days)]


def header_key(data_key: str) -> str:
    return data_key.rsplit("/", 1)[0] + "/_header.csv"


# --------------------------------------------------------------------------
# Firing
# --------------------------------------------------------------------------

_issued: set[str] = set()


def request_id(table: str) -> str:
    """F + first four letters of the table + 8 digits: 13 characters, letters
    then digits -- the only shape SAP has delivered on. Never reused (SAP
    dedupes on it), so a same-second collision steps forward."""
    letters = re.sub(r"[^A-Z0-9]", "", table.upper())
    if letters.startswith("ZMM") and len(letters) > 4:
        letters = letters[3:]  # ZMM_GP_HDR -> GPHD, ZMM_GP_ITEM -> GPIT, ZMM_GP_IN -> GPIN
    stem = "F" + letters[:4]
    seconds = int(time.time()) % 10**REQUEST_ID_DIGITS
    while True:
        candidate = f"{stem}{seconds:0{REQUEST_ID_DIGITS}d}"
        if candidate not in _issued:
            _issued.add(candidate)
            return candidate
        seconds = (seconds + 1) % 10**REQUEST_ID_DIGITS


def extract_path(rid: str, table: str, from_date: str, to_date: str, max_rows: str) -> str:
    key = (f"TableExtractSet(RequestId='{rid}',TabName='{table}',FromDate='{from_date}',"
           f"ToDate='{to_date}',IsDelta='',MaxRows='{max_rows}')")
    return f"sap/opu/odata/SAP/{CSV_SERVICE}/{key}/$value"


def open_requests(config: dict[str, str]) -> dict[str, str] | None:
    """Tables the app has an extract open for. A chunk we trigger for one of
    those would be counted into the app's request and corrupt it."""
    url = config.get("DATABASE_URL")
    if not url:
        return None
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT sap_table, request_id FROM csv_extract_request WHERE status = 'open'"))
            found = {r[0]: r[1] for r in rows}
        engine.dispose()
        return found
    except Exception as exc:
        print(f"  (could not read csv_extract_request: {type(exc).__name__}: {str(exc)[:120]})")
        return None


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


def _blank(value: str) -> bool:
    text = value.strip()
    # 00000000 / 00.00.0000 is SAP's initial (empty) date, not a date.
    return not text or (set(text) <= {"0", ".", "-", ":"} and len(text) >= 6)


def _csv_date(value: str) -> date | None:
    text = value.strip()
    for fmt in ("%Y%m%d", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _csv_number(value: str) -> float | None:
    text = value.strip().replace(",", "")
    negative = text.endswith("-")
    try:
        number = float(text.rstrip("-"))
    except ValueError:
        return None
    return -number if negative else number


@dataclass
class CsvProfile:
    columns: list[str] = field(default_factory=list)
    rows: int = 0
    ragged: int = 0
    duplicates: int = 0
    blank_columns: list[str] = field(default_factory=list)
    duplicate_columns: list[str] = field(default_factory=list)
    fields: dict[str, dict] = field(default_factory=dict)
    plants: dict[str, int] = field(default_factory=dict)
    key_fields: list[str] = field(default_factory=list)
    xkeys: set = field(default_factory=set)


def analyse(table: Table, header: list[str], lines: Any, odata_keys: tuple[str, ...]) -> CsvProfile:
    """One pass over the rows. ``odata_keys`` are the SAP fields matching the
    OData key, for the cross-route spot check."""
    prof = CsvProfile()
    cols = [c.strip().upper() for c in header]
    prof.columns = cols
    for i, name in enumerate(cols):
        if not name:
            before = next((cols[j] for j in range(i - 1, -1, -1) if cols[j]), "(start)")
            prof.blank_columns.append(f"#{i + 1} after {before}")
    prof.duplicate_columns = sorted(n for n, c in Counter(c for c in cols if c).items() if c > 1)
    index = {}
    for i, name in enumerate(cols):
        if name and name not in index:
            index[name] = i
    field_list = [c for c in cols if c] if table.profile_all else list(table.fields)
    wanted = [f for f in field_list if f in index]
    prof.key_fields = [k for k in table.csv_keys if k in index]
    key_idx = [index[k] for k in prof.key_fields]
    x_idx = [index[k] for k in odata_keys if k in index]
    plant_idx = next((index[p] for p in PLANT_FIELDS if p in index), None)
    stats = {f: {"filled": 0, "nonzero": 0, "numeric": 0, "dated": 0, "low": None, "high": None} for f in wanted}
    seen: set = set()
    plants: Counter = Counter()
    width = len(cols)
    for row in lines:
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        if [c.strip().upper() for c in row] == cols:
            continue  # a repeated header that slipped through
        prof.rows += 1
        if len(row) != width:
            prof.ragged += 1
        if key_idx:
            key = tuple(norm_key(row[i]) if i < len(row) else "" for i in key_idx)
            if key in seen:
                prof.duplicates += 1
            else:
                seen.add(key)
        if x_idx:
            prof.xkeys.add(tuple(norm_key(row[i]) if i < len(row) else "" for i in x_idx))
        if plant_idx is not None and plant_idx < len(row):
            plants[row[plant_idx].strip() or "(blank)"] += 1
        for f in wanted:
            i = index[f]
            value = row[i] if i < len(row) else ""
            if _blank(value) if f in DATE_FIELDS else not value.strip():
                continue
            s = stats[f]
            s["filled"] += 1
            if f in NUMERIC_FIELDS:
                number = _csv_number(value)
                if number is not None:
                    s["numeric"] += 1
                    s["nonzero"] += number != 0
            if f in DATE_FIELDS:
                day = _csv_date(value)
                if day:
                    s["dated"] += 1
                    s["low"] = day if s["low"] is None or day < s["low"] else s["low"]
                    s["high"] = day if s["high"] is None or day > s["high"] else s["high"]
    n = prof.rows
    for f in field_list:
        if f not in index:
            prof.fields[f] = {"csv": False}
            continue
        s = stats[f]
        info: dict[str, Any] = {"csv": True, "fill": round(100 * s["filled"] / n, 1) if n else None}
        if f in NUMERIC_FIELDS:
            info["nonzero"] = round(100 * s["nonzero"] / n, 1) if n else None
            if s["filled"] and s["numeric"] < s["filled"]:
                info["unparsable"] = s["filled"] - s["numeric"]
        if f in DATE_FIELDS and s["low"]:
            info["range"] = f"{s['low']}..{s['high']}"
            if s["dated"] < s["filled"]:
                info["unparsable"] = s["filled"] - s["dated"]
        prof.fields[f] = info
    prof.plants = dict(plants.most_common(8))
    return prof


def rows_of(text: str) -> tuple[list[str], Any]:
    reader = csv.reader(io.StringIO(text, newline=""))
    return next(reader, []), reader


def decode(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text.lstrip("﻿").replace("\x00", "")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


@dataclass
class Job:
    table: Table
    rid: str = ""
    ack: str = ""
    fired_ok: bool = False
    fired_at: float = 0.0
    baseline: dict[str, int] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)
    first_growth: float | None = None
    last_growth: float | None = None
    status: str = "pending"  # pending / delivered / timeout / fire-failed / skipped
    note: str = ""


def watch(lake: Lake, jobs: list[Job], *, poll: int, quiet: int, first_timeout: int, max_wait: int) -> None:
    started = time.time()
    while True:
        waiting = [j for j in jobs if j.status == "pending"]
        if not waiting:
            return
        now = time.time()
        for job in waiting:
            for key in landing_candidates(job.table.sap):
                job.baseline.setdefault(key, 0)  # a new day's file starts from zero
                size = lake.size(key)
                if size > job.seen.get(key, job.baseline[key]):
                    grew = size - job.seen.get(key, job.baseline[key])
                    job.seen[key] = size
                    job.first_growth = job.first_growth or now
                    job.last_growth = now
                    print(f"  {datetime.now():%H:%M:%S}  {job.table.sap:<6} +{grew / 1e6:,.1f} MB  -> {key}")
            if job.last_growth and now - job.last_growth >= quiet:
                job.status = "delivered"
            elif not job.first_growth and now - job.fired_at >= first_timeout:
                job.status = "timeout"
                job.note = f"no chunk within {first_timeout // 60} min"
        if now - started > max_wait:
            for job in jobs:
                if job.status == "pending":
                    job.status = "timeout" if not job.first_growth else "delivered"
                    job.note = "stopped at --max-wait" + ("" if job.first_growth else ", nothing arrived")
            return
        remaining = [j.table.sap for j in jobs if j.status == "pending"]
        if remaining:
            print(f"  {datetime.now():%H:%M:%S}  waiting on {len(remaining)}: {' '.join(remaining)}", flush=True)
            time.sleep(poll)


def print_profile(table: Table, entry: dict) -> None:
    who = " ".join(table.initiatives) or (table.descoped or "-")
    head = f"{table.sap:<11} {table.entity_set:<28} {who}   [{entry['status']}]"
    print("\n" + "=" * len(head) + "\n" + head + "\n" + "=" * len(head))
    if entry.get("request_id"):
        print(f"  fired      {entry['request_id']}  {entry.get('ack', '')[:90]}")
    if entry.get("source"):
        print(f"  source     {entry['source']}")
    if entry["status"] not in ("delivered", "inspected"):
        print(f"  result     {entry.get('note') or entry['status']}")
        return
    ref = entry.get("odata_count")
    rec = entry.get("reconcile")
    print(f"  rows       {fmt_n(entry['rows'])} in {entry['columns']} columns  "
          f"(OData $count {fmt_n(ref) if ref is not None else 'n/a'} -> {rec})")
    print(f"  keys       {','.join(entry['key_fields']) or '-'}: {entry['duplicates']:,} duplicate(s);  "
          f"ragged rows {entry['ragged']:,}")
    if entry.get("spot_check"):
        print(f"  spot check {entry['spot_check']}")
    if entry["blank_columns"]:
        print(f"  BLANK HEADERS {len(entry['blank_columns'])}: {', '.join(entry['blank_columns'])}")
    if entry["duplicate_columns"]:
        print(f"  duplicate column names: {', '.join(entry['duplicate_columns'])}")
    print(f"  {'field':<13} {'needed by':<12} {'in csv':<7} {'fill':>6} {'nonzero':>8}  notes")
    for f, info in entry["fields"].items():
        note = table.notes.get(f, "")
        needed = ",".join(table.users_of(f)) or ("all" if table.profile_all else "views")
        if not info["csv"]:
            mark = "MANDATORY GAP" if table.users_of(f) else "not in CSV"
            print(f"  {f:<13} {needed:<12} {'NO':<7} {'':>6} {'':>8}  {mark} {note}")
            continue
        fill = "-" if info.get("fill") is None else ("<1%" if 0 < info["fill"] < 1 else f"{info['fill']:.0f}%")
        nz = "" if info.get("nonzero") is None else f"{info['nonzero']:.0f}%"
        extra = [note, info.get("range", "")]
        if info.get("unparsable"):
            extra.append(f"{info['unparsable']:,} unparsable")
        flag = "  <- EMPTY" if info.get("fill") == 0 else ""
        print(f"  {f:<13} {needed:<12} {'yes':<7} {fill:>6} {nz:>8}  {' '.join(x for x in extra if x)}{flag}")
    if entry.get("plants"):
        print("  plants     " + "  ".join(f"{k}: {v:,}" for k, v in entry["plants"].items()))


def coverage(report: dict, odata: dict | None) -> None:
    """Per initiative: is every mandatory field served, with data, by a SAP
    route -- OData, CSV or both? EKKO/EKET on the workbook fallback are not
    SAP routes, so their fields only count when a route delivers them."""
    print("\nINITIATIVE COVERAGE  " + ("(OData report + this CSV run)" if odata else "(CSV only -- no OData report found; pass --odata-report for both routes)"))
    matrix: dict[str, dict[str, str]] = {}
    for sap, entry in report["tables"].items():
        table = BY_SAP[sap]
        o_entry = (odata or {}).get("tables", {}).get(sap, {})
        o_fields = o_entry.get("fields", {})
        o_unreadable = bool(o_entry.get("read", {}).get("error")) or bool(o_entry.get("error_meta"))
        c_undelivered = entry.get("status") not in ("delivered", "inspected")
        for f in entry.get("fields", {}):
            c = entry["fields"].get(f, {})
            o = o_fields.get(f, {})
            in_csv = bool(c.get("csv")) and (c.get("fill") or 0) > 0
            in_odata = bool(o.get("odata")) and (o.get("fill") or 0) > 0
            present = bool(c.get("csv")) or bool(o.get("odata"))
            blocked = (o_unreadable or not o.get("odata")) and (c_undelivered or not c.get("csv"))
            source = ("both" if in_csv and in_odata else "CSV" if in_csv else "OData" if in_odata
                      else "UNREADABLE" if blocked and present else "EMPTY" if present else "GAP")
            matrix.setdefault(sap, {})[f] = source
    for initiative in INITIATIVES:
        tally: Counter = Counter()
        problems: list[str] = []
        other = other_ok = 0
        for sap, fields in matrix.items():
            table = BY_SAP[sap]
            if table.descoped:
                continue
            for f, source in fields.items():
                if initiative in table.users_of(f):
                    tally[source] += 1
                    if source in ("GAP", "EMPTY", "UNREADABLE"):
                        problems.append(f"{sap}.{f}({source.lower()})")
                elif initiative in table.initiatives and not table.users_of(f):
                    other += 1
                    other_ok += source in ("both", "CSV", "OData")
        mandatory = sum(tally.values())
        verdict = ("n/a (no mandatory field in this run)" if not mandatory
                   else "PASS -- every mandatory field comes from SAP" if not problems else "FAIL")
        print(f"  {initiative}: mandatory {mandatory} -- both routes {tally['both']}, OData only {tally['OData']}, "
              f"CSV only {tally['CSV']}, unreadable {tally['UNREADABLE']}, empty {tally['EMPTY']}, "
              f"missing {tally['GAP']} -> {verdict}; "
              f"other fields the views read: {other_ok}/{other}")
        if problems:
            print("       " + " ".join(problems))
    checked = {sap for sap in report["tables"] if not BY_SAP[sap].descoped}
    absent = [t.sap for t in CATALOGUE if not t.descoped and t.sap not in checked]
    if absent:
        print(f"  (not in this run, so not judged: {' '.join(absent)})")
    report["coverage"] = matrix


def latest_odata_report() -> Path | None:
    folder = default_out("x").parent
    candidates = sorted(folder.glob("odata_*.json"))
    return candidates[-1] if candidates else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tables", nargs="+", metavar="SAP", help="e.g. EKPO MSEG (default: all 21)")
    parser.add_argument("--inspect", action="store_true",
                        help="do not fire: analyse the latest landed file per table (from any earlier pull)")
    parser.add_argument("--from-date", default=FULL_FROM, help=f"extract window start (default {FULL_FROM} = full)")
    parser.add_argument("--to-date", default=FULL_TO, help=f"extract window end (default {FULL_TO})")
    parser.add_argument("--max-rows", default="", help="cap per table, e.g. 100 (default: none)")
    parser.add_argument("--gap", type=int, default=3, help="seconds between fires (default 3)")
    parser.add_argument("--batch-size", type=int, default=2, metavar="N",
                        help="fire N tables, wait until they are delivered, then the next N "
                             "(default 2; 0 = fire every table at once)")
    parser.add_argument("--poll", type=int, default=15, help="seconds between storage checks (default 15)")
    parser.add_argument("--quiet", type=int, default=120,
                        help="no growth for this long = delivered (default 120; SAP sends a chunk every ~9s, "
                             "so a pause longer than this -- e.g. the app restarting -- ends the wait early)")
    parser.add_argument("--first-chunk-timeout", type=int, default=300, help="nothing by then = timeout (default 300)")
    parser.add_argument("--max-wait", type=int, default=1800, help="hard stop for each batch's wait (default 1800)")
    parser.add_argument("--force", action="store_true", help="fire even where the app has an extract open")
    parser.add_argument("--odata-report", help="JSON from odata_live_check.py (default: the newest one in the report folder)")
    parser.add_argument("--coverage-from", metavar="CSV_JSON",
                        help="no firing, no reading: print the coverage again from this run's saved report "
                             "(e.g. after the OData report was produced later)")
    parser.add_argument("--services", nargs="+", default=list(DEFAULT_SERVICES))
    parser.add_argument("--env-file", help="read configuration from this .env instead of ./.env")
    parser.add_argument("--out", help="JSON report path (default: /home/live_checks/csv_<time>.json)")
    args = parser.parse_args(argv)

    wanted = [t.upper() for t in args.tables] if args.tables else [t.sap for t in CATALOGUE]
    unknown = [t for t in wanted if t not in BY_SAP]
    if unknown:
        parser.error(f"unknown table(s) {unknown}; known: {' '.join(BY_SAP)}")
    full_window = (args.from_date, args.to_date) == (FULL_FROM, FULL_TO) and not args.max_rows

    if args.coverage_from:
        saved = json.loads(Path(args.coverage_from).read_text(encoding="utf-8"))
        odata_path = Path(args.odata_report) if args.odata_report else latest_odata_report()
        odata = json.loads(odata_path.read_text(encoding="utf-8")) if odata_path and odata_path.is_file() else None
        print(f"coverage from {args.coverage_from}" + (f" + {odata_path}" if odata else " (no OData report)"))
        coverage(saved, odata)
        return 0

    config = load_config(args.env_file)
    cpi = Cpi(config)
    lake = Lake(config)
    started = time.time()
    mode = "inspect latest landed files" if args.inspect else f"fire {args.from_date}..{args.to_date}"
    print(f"CSV live check  {datetime.now():%Y-%m-%d %H:%M}  {mode}  endpoint {cpi.endpoint}")
    cpi.token()
    metas, meta_errors = fetch_metadata(cpi, tuple(args.services))
    for service, error in meta_errors.items():
        print(f"$metadata  {service}: FAIL {error} (no reconciliation for its tables)")

    jobs = [Job(BY_SAP[t]) for t in wanted]

    if not args.inspect:
        busy = open_requests(config)
        if busy is None:
            print("open-request guard: DATABASE_URL not readable here -- make sure no `python -m app.ingest "
                  "--csv-pull` is running, or its counts will absorb these chunks")
        for job in jobs:
            if busy and job.table.sap in busy and not args.force:
                job.status, job.note = "skipped", f"the app has request {busy[job.table.sap]} open for it (--force to fire anyway)"
        for job in jobs:
            if job.status != "pending":
                print(f"  {job.table.sap:<6} skipped: {job.note}")
        to_fire = [j for j in jobs if j.status == "pending"]
        size = args.batch_size if 0 < args.batch_size < len(to_fire) else len(to_fire)
        batches = [to_fire[start:start + size] for start in range(0, len(to_fire), size)] if to_fire else []
        print(f"\nFIRING  {len(to_fire)} table(s), {args.gap}s apart"
              + (f", in {len(batches)} batches of up to {size} -- each delivered before the next"
                 if len(batches) > 1 else ""))
        for number, batch in enumerate(batches, 1):
            if len(batches) > 1:
                print(f"\nBATCH {number}/{len(batches)}  {' '.join(j.table.sap for j in batch)}")
            for index, job in enumerate(batch, 1):
                for key in landing_candidates(job.table.sap):
                    job.baseline[key] = job.seen[key] = lake.size(key)
                job.rid = request_id(job.table.sap)
                reply = cpi.get(extract_path(job.rid, job.table.sap, args.from_date, args.to_date, args.max_rows), "")
                job.fired_at = time.time()
                job.ack = " ".join(reply.text.split())[:200] if reply.ok else short_error(reply)
                job.fired_ok = reply.ok and "success" in reply.text.lower()
                if not job.fired_ok:
                    job.status, job.note = "fire-failed", job.ack
                print(f"  {job.table.sap:<6} {job.rid}  HTTP {reply.status}  {job.ack[:100]}")
                if index < len(batch):
                    time.sleep(args.gap)
            print(f"\nWAITING  delivered = {args.quiet}s with no growth; timeout = nothing in "
                  f"{args.first_chunk_timeout // 60} min")
            watch(lake, batch, poll=args.poll, quiet=args.quiet, first_timeout=args.first_chunk_timeout,
                  max_wait=args.max_wait)
            if len(batches) > 1:
                print("  " + "  ".join(f"{j.table.sap} {j.status}" for j in batch))

    report: dict[str, Any] = {
        "run": {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": mode,
                "window": [args.from_date, args.to_date], "max_rows": args.max_rows},
        "tables": {},
    }
    failures = 0
    for job in jobs:
        table = job.table
        entry: dict[str, Any] = {"status": job.status, "note": job.note, "request_id": job.rid, "ack": job.ack,
                                 "fields": {f: {"csv": False} for f in table.fields}}
        text = ""
        if args.inspect:
            key = lake.latest(table.sap)
            if key is None:
                entry.update(status="nothing landed", note=f"no {table.sap}.csv under {CSV_PREFIX}/{table.sap}/")
            else:
                entry.update(status="inspected", source=key)
                text = decode(lake.read(key))
                header, lines = rows_of(text)
        elif job.status == "delivered":
            parts = []
            header: list[str] = []
            for key, size in job.seen.items():
                start = job.baseline.get(key, 0)
                if size <= start:
                    continue
                chunk = decode(lake.read(key, offset=start))
                if start == 0:
                    first, _, chunk = chunk.partition("\n")
                    header = header or next(csv.reader([first.rstrip(chr(13))]))
                elif not header:
                    header = next(csv.reader([decode(lake.read(header_key(key))).splitlines()[0]]))
                parts.append(chunk)
                entry.setdefault("source", "")
                entry["source"] = (entry["source"] + " " + f"{key} (from byte {start:,})").strip()
            text = "".join(parts)
            lines = csv.reader(io.StringIO(text, newline=""))
        if text and entry["status"] in ("delivered", "inspected"):
            meta = metas.get(table.entity_set)
            odata_keys = table.spot_keys or tuple(
                f for f in (next((s for s in table.csv_keys if norm(s) == norm(k)), None) for k in (meta.keys if meta else ()))
                if f
            )
            prof = analyse(table, header, lines, odata_keys)
            entry.update(rows=prof.rows, columns=len(prof.columns), ragged=prof.ragged, duplicates=prof.duplicates,
                         key_fields=prof.key_fields, blank_columns=prof.blank_columns,
                         duplicate_columns=prof.duplicate_columns, fields=prof.fields, plants=prof.plants)
            if meta is not None:
                ceiling = bool(table.count_parts)
                if ceiling:
                    parts = [count(cpi, meta, f) for f in table.count_parts]
                    ref = sum(n for n, _ in parts) if all(n is not None for n, _ in parts) else None
                    error = next((e for _, e in parts if e), None)
                else:
                    ref, error = count(cpi, meta, table.reference_filter)
                entry["odata_count"] = ref
                diff = None if ref is None else prof.rows - ref
                if ref is None:
                    entry["reconcile"] = f"no reference ({error})"
                elif ceiling:
                    # The extract sends every object class; OData can only count the
                    # classes we name, so more rows than that is information, not a fault.
                    entry["reconcile"] = (f"OK ({len(table.count_parts)} counted classes = {ref:,})" if diff <= 0
                                          else f"INFO: {diff:,} rows beyond the {len(table.count_parts)} counted classes")
                elif not full_window or args.inspect:
                    entry["reconcile"] = ("OK (within)" if diff <= 0 else f"OVER by {diff:,}")
                elif diff == 0:
                    entry["reconcile"] = "EXACT"
                elif abs(diff) <= max(2, ref // 10_000):
                    entry["reconcile"] = f"WITHIN {abs(diff)} row(s) (live system changed during the run)"
                else:
                    entry["reconcile"] = f"OFF by {diff:+,}"
                if odata_keys:
                    sample, error = read_page(cpi, meta, filter=table.reference_filter, order_by=meta.keys, top=25, skip=0)
                    if sample is None:
                        sample, error = read_page(cpi, meta, filter=table.reference_filter, order_by=meta.keys[:1], top=25, skip=0)
                    if sample:
                        props = [odata_property(meta, f) for f in odata_keys]
                        keys = [tuple(norm_key(r.get(p)) for p in props) for r in sample]
                        found = sum(k in prof.xkeys for k in keys)
                        entry["spot_check"] = f"{found}/{len(keys)} of the first OData keys ({','.join(odata_keys)}) found in the CSV"
                        if not found and keys and prof.xkeys:
                            # Almost always a format difference (ISO vs SAP language key,
                            # MM.YYYY vs YYYYMM), not missing rows: show one of each.
                            entry["spot_check"] += (f"; formats differ? OData {'|'.join(keys[0])!r} "
                                                    f"vs CSV {'|'.join(sorted(prof.xkeys)[0])!r}")
                    else:
                        entry["spot_check"] = f"OData sample unavailable ({error})"
            if prof.duplicates or (entry.get("reconcile") or "").startswith(("OFF", "OVER")):
                failures += 1
        elif entry["status"] not in ("skipped",):
            failures += 1
        report["tables"][table.sap] = entry
        print_profile(table, entry)

    odata = None
    odata_path = Path(args.odata_report) if args.odata_report else latest_odata_report()
    if odata_path and odata_path.is_file():
        odata = json.loads(odata_path.read_text(encoding="utf-8"))
        print(f"\nOData report used for coverage: {odata_path}")
    coverage(report, odata)

    print("\nSUMMARY")
    for sap, entry in report["tables"].items():
        rows = fmt_n(entry.get("rows")) if entry.get("rows") is not None else "-"
        print(f"  {sap:<6} {entry['status']:<15} rows {rows:>9}  {entry.get('reconcile') or entry.get('note') or ''}")
    report["run"].update(seconds=round(time.time() - started), cpi_calls=cpi.calls, failures=failures)
    out = Path(args.out) if args.out else default_out("csv")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n{time.time() - started:.0f}s; {failures} table(s) not delivered, not reconciled or with duplicate keys.")
    print(f"report: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
