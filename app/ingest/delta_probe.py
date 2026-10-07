"""Measure every declared delta against live SAP, and write the evidence.

    python -m app.ingest.delta_probe                 # writes discovery/delta_support.csv
    python -m app.ingest.delta_probe --out FILE.csv  # anywhere else
    python -m app.ingest.delta_probe --set POHistorySet

Read-only: ``$count`` and GET requests through CPI, nothing else.

WHY THIS EXISTS

``manifest.check_delta_filters`` refuses to run a delta whose filter is not
measured HONOURED, because a filter SAP ignores answers HTTP 200 with the
whole set and the "increment" silently becomes a full pull. The discovery
sweep's impossible-value probe (filter_support.csv) cannot settle that for a
date SAP re-typed to Edm.String: it sends ``'ZZ~NOPE'``, gets HTTP 400 for a
value no date can hold, and records a rejection -- or, on MSEG, gets 0 and
records HONOURED for a field that ignores the very literal shape the pipeline
would have sent. This probe sends exactly what fetch.py sends, built by the
same function, and records what came back.

THE TEST, PER DELTA FIELD

    total        $count with only the predicate the set demands
    impossible   field eq <1900-01-01>                      must be 0
    real         field eq <the newest day in the window>    must equal the
                 rows of that day the window returned, and be a proper subset
    window       field ge <a recent day>                    every row read must
                 fall on or after it, and as many must be read as $count said

HONOURED only when all four hold. And per parent key -- the `or` chain a
derived read sends -- that a three-key chain returns exactly the rows of
those three keys and nothing else.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.ingest.fetch import combine, delta_literal, odata_literal, window_filter
from app.ingest.manifest import DELTA_SUPPORT_FILE, DELTAS, HONOURED, KEY_CHAIN, IngestSpec, spec_for
from app.ingest.watermarks import as_day
from app.integrations.sap.client import SapClient
from app.integrations.sap.contract import discovery_dir

FIELDS = (
    "measured_at", "service", "entity_set", "property", "literal", "required_filter",
    "total", "impossible", "real_day", "real", "real_expected",
    "window_from", "window", "window_read", "outside", "verdict", "note",
)
NOT_HONOURED = "NOT_HONOURED"
IMPOSSIBLE_DAY = "1900-01-01"

# How far back a window reaches before it holds rows. DEV is sparse: most
# sets have a handful of documents in the last month and thousands from 2013.
WINDOW_DAYS = (30, 180, 730, 3650)


def _count(client: SapClient, spec: IngestSpec, expression: str | None) -> int | str:
    try:
        value = client.count(spec.name, filter=expression, allow_unsupported_filter=True)
    except Exception as exc:  # noqa: BLE001 -- the failure is the measurement
        return f"error: {type(exc).__name__}: {str(exc)[:120]}"
    return "no $count" if value is None else value


def probe_window(client: SapClient, spec: IngestSpec, today: date) -> dict:
    """The four-step test for a set's own delta field."""
    delta = spec.delta
    assert delta is not None and delta.field is not None
    row = {
        "service": spec.service, "entity_set": spec.name, "property": delta.field,
        "literal": delta.literal, "required_filter": spec.required_filter or "",
    }
    required = spec.required_filter
    row["total"] = total = _count(client, spec, required)
    row["impossible"] = _count(
        client, spec, combine(required, f"{delta.field} eq {delta_literal(IMPOSSIBLE_DAY, delta.literal)}")
    )

    window_from, expected = None, None
    for days in WINDOW_DAYS:
        start = (today - timedelta(days=days)).isoformat()
        counted = _count(client, spec, window_filter(spec, start))
        if isinstance(counted, int) and counted > 0:
            window_from, expected = start, counted
            break
    if window_from is None:
        row.update(verdict=NOT_HONOURED, note=f"no rows in any window up to {WINDOW_DAYS[-1]} days back")
        return row
    row["window_from"], row["window"] = window_from, expected

    try:
        extract = client.read_all(spec.name, filter=window_filter(spec, window_from), allow_unsupported_filter=True)
    except Exception as exc:  # noqa: BLE001
        row.update(verdict=NOT_HONOURED, note=f"window read failed: {type(exc).__name__}: {str(exc)[:120]}")
        return row
    days = [as_day(r.get(delta.field)) for r in extract.rows]
    floor = date.fromisoformat(window_from)
    row["window_read"] = len(extract.rows)
    row["outside"] = sum(1 for d in days if d is None or d < floor)

    newest = max((d for d in days if d is not None), default=None)
    if newest is not None:
        row["real_day"] = newest.isoformat()
        row["real_expected"] = sum(1 for d in days if d == newest)
        row["real"] = _count(
            client, spec, combine(required, f"{delta.field} eq {delta_literal(newest, delta.literal)}")
        )

    honoured = (
        isinstance(total, int)
        and row["impossible"] == 0
        and isinstance(row.get("real"), int)
        and 0 < row["real"] == row.get("real_expected") < total
        and row["window_read"] == expected < total
        and row["outside"] == 0
    )
    row["verdict"] = HONOURED if honoured else NOT_HONOURED
    return row


def probe_keys(client: SapClient, spec: IngestSpec) -> dict:
    """A three-key `or` chain returns exactly those keys' rows."""
    delta = spec.delta
    assert delta is not None and delta.via_key is not None
    key = delta.via_key
    row = {
        "service": spec.service, "entity_set": spec.name, "property": key,
        "literal": KEY_CHAIN, "required_filter": spec.required_filter or "",
    }
    required = spec.required_filter
    row["total"] = total = _count(client, spec, required)
    try:
        first = client.read(spec.name, filter=required, top=200, order_by=[key], allow_unsupported_filter=True)
        chosen = list(dict.fromkeys(str(r[key]) for r in first.rows if r.get(key)))[:3]
        if len(chosen) < 3:
            row.update(verdict=NOT_HONOURED, note="fewer than three keys to chain")
            return row
        each = [_count(client, spec, combine(required, f"{key} eq {odata_literal(k, 'Edm.String')}")) for k in chosen]
        chain = combine(required, " or ".join(f"{key} eq {odata_literal(k, 'Edm.String')}" for k in chosen))
        extract = client.read_all(spec.name, filter=chain, allow_unsupported_filter=True)
    except Exception as exc:  # noqa: BLE001
        row.update(verdict=NOT_HONOURED, note=f"read failed: {type(exc).__name__}: {str(exc)[:120]}")
        return row
    row["window_from"] = " | ".join(chosen)
    row["real_expected"] = sum(e for e in each if isinstance(e, int))
    row["window"] = _count(client, spec, chain)
    row["window_read"] = len(extract.rows)
    row["outside"] = sum(1 for r in extract.rows if str(r.get(key)) not in chosen)
    honoured = (
        isinstance(total, int)
        and all(isinstance(e, int) for e in each)
        and row["window"] == row["window_read"] == row["real_expected"]
        and 0 < row["window_read"] < total
        and row["outside"] == 0
    )
    row["verdict"] = HONOURED if honoured else NOT_HONOURED
    return row


def probe_all(names: list[str] | None = None, *, client: SapClient | None = None) -> list[dict]:
    client = client or SapClient()
    today = datetime.now(timezone.utc).date()
    measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for name in names or sorted(DELTAS):
        spec = spec_for(name)
        delta = spec.delta
        if delta is None:
            continue
        if delta.direct:
            rows.append({"measured_at": measured_at, **probe_window(client, spec, today)})
        if delta.derived:
            rows.append({"measured_at": measured_at, **probe_keys(client, spec)})
    return rows


def write(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=None, help=f"default: discovery/{DELTA_SUPPORT_FILE}")
    parser.add_argument("--set", action="append", dest="sets", help="probe only this entity set (repeatable)")
    args = parser.parse_args(argv)

    rows = probe_all(args.sets)
    out = args.out or discovery_dir() / DELTA_SUPPORT_FILE
    write(rows, out)
    for row in rows:
        print(
            f"{row['entity_set']:28} {row['property']:10} {row['literal']:9} "
            f"total={row.get('total')} impossible={row.get('impossible', '')} "
            f"real={row.get('real', '')}/{row.get('real_expected', '')} "
            f"window={row.get('window', '')} read={row.get('window_read', '')} "
            f"outside={row.get('outside', '')} -> {row['verdict']}"
            + (f"  ({row['note']})" if row.get("note") else "")
        )
    print(f"\n{out}")
    return 0 if all(r["verdict"] == HONOURED for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
