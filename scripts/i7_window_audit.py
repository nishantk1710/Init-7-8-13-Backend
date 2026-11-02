"""What the rolling 24-month demand window changes. Read-only: nothing here writes a table.

    python -m scripts.i7_window_audit window                 # window verdict + eligibility, before vs after
    python -m scripts.i7_window_audit runs                   # the latest two inventory runs, side by side
    python -m scripts.i7_window_audit runs --before 11 --after 14

``window`` runs the feature builder's own row function twice over the real staged data -- once as it built
before the window existed, once with it -- and compares the two. It answers, without a rebuild: which months
the window trusts, who is still classified, who moves to cold-start or MISSING_DATA, what forecasting would
take, and that OAR's rows come out identical.

``runs`` compares two **inventory runs** -- run the pipeline before the change and again after, then point this
at both. It reports who still gets a Safety Stock / ROP / Max Stock, and the working-capital movement
(``quantity x unit price`` summed over the material-plants evaluable in both). Quantities are read as the
inventory stage wrote them; nothing is recomputed.

The comparison functions are pure (rows in, numbers out) and covered by ``tests/i7/test_window_audit.py``; only
the two loaders touch the database, so only they need the App Service.
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
from typing import Any, Mapping

Row = Mapping[str, Any]
Key = tuple[str, str]

OK_STATUSES = ("SUCCESS", "SUCCESS_FROM_CURRENT_SAP_VALUE")


def _is_oar(row: Row) -> bool:
    return row["oar_scope"] == "IN_SCOPE"


def _forecastable(row: Row) -> bool:
    """What forecasting selects: a routed, non-smooth, non-OAR material-plant with sufficient history."""
    return (
        not _is_oar(row)
        and row["history_status"] == "SUFFICIENT"
        and row["baseline_model"] is not None
        and row["demand_class"] != "SMOOTH"
    )


def summarise_eligibility(before: Mapping[Key, Row], after: Mapping[Key, Row]) -> dict[str, Any]:
    """Compare the feature rows built without the window (``before``) and with it (``after``)."""
    keys = [k for k in before if k in after]
    stock = [k for k in keys if not _is_oar(before[k])]
    oar = [k for k in keys if _is_oar(before[k])]

    gate_before = Counter(before[k]["history_status"] for k in stock)
    gate_after = Counter(after[k]["window_history_status"] or after[k]["history_status"] for k in stock)

    transitions: Counter[str] = Counter()
    class_changes: Counter[str] = Counter()
    for k in stock:
        was, now = before[k]["history_status"], after[k]["window_history_status"] or after[k]["history_status"]
        if was != now:
            transitions[f"{was} -> {now}"] += 1
        elif was == "SUFFICIENT" and before[k]["demand_class"] != after[k]["demand_class"]:
            class_changes[f'{before[k]["demand_class"]} -> {after[k]["demand_class"]}'] += 1

    demand_before = demand_after = spread_before = spread_after = Decimal(0)
    compared = 0
    for k in stock:
        price = before[k].get("unit_price")
        if price is None or before[k]["history_status"] != "SUFFICIENT":
            continue
        if (after[k]["window_history_status"] or after[k]["history_status"]) != "SUFFICIENT":
            continue
        price = Decimal(price)
        compared += 1
        demand_before += price * Decimal(before[k]["mean_demand_all_periods"] or 0)
        demand_after += price * Decimal(after[k]["mean_demand_all_periods"] or 0)
        spread_before += price * Decimal(before[k]["std_dev_demand_all_periods"] or 0)
        spread_after += price * Decimal(after[k]["std_dev_demand_all_periods"] or 0)

    return {
        "material_plants": len(keys),
        "oar_material_plants": len(oar),
        "oar_rows_changed": sum(1 for k in oar if dict(before[k]) != dict(after[k])),
        "stock_material_plants": len(stock),
        "gate_before": dict(sorted(gate_before.items())),
        "gate_after": dict(sorted(gate_after.items())),
        "gate_transitions": dict(transitions),
        "class_before": dict(sorted(Counter(before[k]["demand_class"] for k in stock).items())),
        "class_after": dict(sorted(Counter(after[k]["demand_class"] for k in stock).items())),
        "class_changes": dict(class_changes.most_common()),
        "forecastable_before": sum(1 for k in stock if _forecastable(before[k])),
        "forecastable_after": sum(
            1 for k in stock if _forecastable(after[k]) and after[k]["window_history_status"] in (None, "SUFFICIENT")
        ),
        "oar_donors_before": sum(1 for k in stock if before[k]["history_status"] == "SUFFICIENT"),
        "oar_donors_after": sum(1 for k in stock if after[k]["history_status"] == "SUFFICIENT"),
        "driver_compared": compared,
        "driver_demand_value": (demand_before, demand_after),
        "driver_spread_value": (spread_before, spread_after),
    }


def diff_inventory(before: Mapping[Key, Row], after: Mapping[Key, Row]) -> dict[str, Any]:
    """Compare two inventory runs' rows: who is evaluable, and what moved (``quantity x unit price``)."""

    def evaluable(row: Row, prefix: str) -> bool:
        return row[f"{prefix}_status"] in OK_STATUSES and row[prefix] is not None

    out: dict[str, Any] = {"material_plants_before": len(before), "material_plants_after": len(after)}
    for prefix in ("safety_stock", "rop", "max_stock"):
        both = [k for k in before if k in after and evaluable(before[k], prefix) and evaluable(after[k], prefix)]
        value_before = value_after = Decimal(0)
        up = down = same = 0
        for k in both:
            price = Decimal(before[k].get("price") or 0)
            b, a = Decimal(before[k][prefix]), Decimal(after[k][prefix])
            value_before += price * b
            value_after += price * a
            up, down, same = up + (a > b), down + (a < b), same + (a == b)
        out[prefix] = {
            "evaluable_before": sum(1 for r in before.values() if evaluable(r, prefix)),
            "evaluable_after": sum(1 for r in after.values() if evaluable(r, prefix)),
            "newly_evaluable": sum(
                1 for k, r in after.items() if evaluable(r, prefix) and not (k in before and evaluable(before[k], prefix))
            ),
            "no_longer_evaluable": sum(
                1 for k, r in before.items() if evaluable(r, prefix) and not (k in after and evaluable(after[k], prefix))
            ),
            "compared": len(both),
            "increased": up,
            "decreased": down,
            "unchanged": same,
            "value_before": value_before,
            "value_after": value_after,
        }
    return out


def _pct(before: Decimal, after: Decimal) -> str:
    return f"{(after - before) / before:+.1%}" if before else "n/a"


def print_eligibility(summary: dict[str, Any]) -> None:
    print("\n=== Eligibility: built without the window  ->  built with it ===")
    print(f"material-plants compared      : {summary['material_plants']:,}  "
          f"(stock {summary['stock_material_plants']:,}, OAR {summary['oar_material_plants']:,})")
    print(f"OAR rows that changed         : {summary['oar_rows_changed']:,}   <- must be 0")
    print(f"OAR donors (history SUFFICIENT): {summary['oar_donors_before']:,} -> {summary['oar_donors_after']:,}   "
          "<- must be equal")
    print("stock history gate  before    :", summary["gate_before"])
    print("stock history gate  window    :", summary["gate_after"])
    print("gate transitions              :", summary["gate_transitions"] or "none")
    print("demand class before           :", summary["class_before"])
    print("demand class window           :", summary["class_after"])
    print("class changes (still classed) :", summary["class_changes"] or "none")
    print(f"forecast candidates           : {summary['forecastable_before']:,} -> {summary['forecastable_after']:,}")
    d0, d1 = summary["driver_demand_value"]
    s0, s1 = summary["driver_spread_value"]
    print(f"\nstock-figure drivers over {summary['driver_compared']:,} material-plants classified both ways "
          "(price x quantity; direction only, not the final rand figure):")
    print(f"  demand rate value  (drives ROP / max) : {d0:>16,.0f} -> {d1:>16,.0f}  ({_pct(d0, d1)})")
    print(f"  spread value       (drives safety)    : {s0:>16,.0f} -> {s1:>16,.0f}  ({_pct(s0, s1)})")


def print_inventory(summary: dict[str, Any]) -> None:
    print(f"\n=== Inventory runs: {summary['material_plants_before']:,} -> {summary['material_plants_after']:,} "
          "material-plants ===")
    for prefix, label in (("safety_stock", "Safety stock"), ("rop", "ROP"), ("max_stock", "Max stock")):
        s = summary[prefix]
        print(f"\n{label}: evaluable {s['evaluable_before']:,} -> {s['evaluable_after']:,}  "
              f"(+{s['newly_evaluable']:,} new, -{s['no_longer_evaluable']:,} lost)")
        print(f"  evaluable in both: {s['compared']:,}  "
              f"up {s['increased']:,} / down {s['decreased']:,} / same {s['unchanged']:,}")
        print(f"  working capital (qty x price): {s['value_before']:>16,.0f} -> {s['value_after']:>16,.0f}  "
              f"({_pct(s['value_before'], s['value_after'])})")


# --- loaders: the only part that touches the database ------------------------------------------------------------


def run_window() -> None:
    from sqlalchemy import text

    from app.core.db import get_sessionmaker
    from app.initiatives.i7.features import builder
    from app.initiatives.i7.features.consumption_window import resolve_window_for_run
    from app.initiatives.i7.policy import PolicyDocument

    policy = PolicyDocument()
    with get_sessionmaker()() as session:
        run = builder.resolve_staging_run(session)
        window = resolve_window_for_run(session, run.id, policy.consumption_window)

        print(f"staging run {run.id}: {window.describe()}")
        print(f"status {window.status.value}; window "
              f"{window.start:%Y-%m} .. {window.end:%Y-%m}" if window.range else f"status {window.status.value}")
        print("\nmonth     status        movements  reference  why")
        for a in window.assessments[-30:]:
            ref = f"{a.reference:>9,.0f}" if a.reference is not None else "        -"
            print(f"{a.month:%Y-%m}   {a.status.value:<12} {a.volume:>10,} {ref}  {a.reason}")

        consumption = builder._load_consumption(session, run.id)
        events = builder._load_consumption_event_counts(session, run.id)
        purchase_orders = builder._load_purchase_order_counts(session)
        stock = builder._load_stock(session)
        legacy_window = builder.observation_window(session, run.id)
        attributes = session.execute(text(builder._ATTRIBUTE_SQL), {"not_deleted": False}).all()

        before: dict[Key, dict] = {}
        after: dict[Key, dict] = {}
        for row in attributes:
            key = (row.sap_material_number, row.sap_plant_code)
            for target, use in ((before, None), (after, window)):
                target[key] = builder._build_one(
                    row, consumption, events, purchase_orders, stock, legacy_window, policy, 0,
                    builder.FeatureBuildResult(), consumption_window=use,
                )

    print_eligibility(summarise_eligibility(before, after))


def run_inventory(before_id: int | None, after_id: int | None) -> None:
    from sqlalchemy import select, text

    from app.core.db import get_sessionmaker
    from app.models.i7_inventory import InventoryRun

    sql = text(
        "SELECT i.sap_material_number AS material, i.sap_plant_code AS plant, i.safety_stock, i.rop, "
        "i.max_stock, i.safety_stock_status, i.rop_status, i.max_stock_status, f.unit_price AS price "
        "FROM i7_inventory_calculation i "
        "LEFT JOIN i7_material_feature f ON f.sap_material_number = i.sap_material_number "
        "AND f.sap_plant_code = i.sap_plant_code "
        "WHERE i.inventory_run_id = :run"
    )
    with get_sessionmaker()() as session:
        if before_id is None or after_id is None:
            ids = session.execute(
                select(InventoryRun.id).where(InventoryRun.status == "succeeded").order_by(InventoryRun.id.desc()).limit(2)
            ).scalars().all()
            if len(ids) < 2:
                raise SystemExit("need two succeeded inventory runs; run the pipeline before and after the change")
            after_id, before_id = ids
        print(f"inventory run {before_id} (before)  vs  {after_id} (after)")
        before = {(r.material, r.plant): dict(r._mapping) for r in session.execute(sql, {"run": before_id})}
        after = {(r.material, r.plant): dict(r._mapping) for r in session.execute(sql, {"run": after_id})}
    print_inventory(diff_inventory(before, after))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("window", help="window verdict and eligibility, built with and without the window")
    runs = sub.add_parser("runs", help="two inventory runs, side by side")
    runs.add_argument("--before", type=int)
    runs.add_argument("--after", type=int)
    args = parser.parse_args()
    if args.mode == "window":
        run_window()
    else:
        run_inventory(args.before, args.after)


if __name__ == "__main__":
    main()
