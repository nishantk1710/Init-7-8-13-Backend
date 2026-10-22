"""Compare the weighted-median and weighted-mean OAR estimates on STORED neighbours.

    python -m scripts.compare_oar_estimators                      # the latest succeeded OAR run
    python -m scripts.compare_oar_estimators --run-id 41 --top 25 --csv differences.csv

For every target with a successful estimate it re-derives both estimates from the neighbour rows already stored
for it -- each donor's stored combined similarity (the weight) and the SAP values it lent -- and reports where
they differ. It READS ``i7_oar_target`` and ``i7_oar_neighbour`` and nothing else; it writes nothing, to the
database or anywhere but the optional ``--csv``.

Why it exists: ``oar-similarity-10`` replaced the similarity-weighted mean by the weighted median. The unit
tests prove the arithmetic on fixtures; this is how to see what the change does to a real run -- how many
estimates move, by how much, and which ones -- before anyone relies on it. It needs the database, so like every
DB-backed check it runs from inside the network.

Targets compared here are those whose estimate SUCCEEDED, so a target made NOT_EVALUABLE by the identity stage
under the new rules has no neighbours stored under the OLD run and is not in this comparison. For that, compare
two runs (``algorithm_version`` oar-similarity-9 against -10) by their ``i7_oar_target`` rows.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Iterable, Sequence

from app.initiatives.i7.oar.estimate import (
    DEFAULT_MATERIAL_DIFFERENCE_RATIO,
    apply_max_floor,
    weighted_mean,
    weighted_median,
)

FIELDS = ("safety_stock", "rop", "max_stock")


@dataclass(frozen=True)
class StoredDonor:
    """One neighbour row as stored: its weight and the SAP values it lent."""

    weight: Decimal
    safety_stock: int | None
    rop: int | None
    max_stock: int | None


@dataclass(frozen=True)
class Comparison:
    material: str
    plant: str
    donors: int
    mean: dict[str, int | None]
    median: dict[str, int | None]
    material_difference: bool
    """Some parameter differs by at least the diagnostic ratio of the mean."""

    @property
    def rop_delta(self) -> int | None:
        if self.mean["rop"] is None or self.median["rop"] is None:
            return None
        return self.median["rop"] - self.mean["rop"]


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _estimate(donors: Sequence[StoredDonor], strategy) -> dict[str, int | None]:
    """The three parameters under one strategy, rounded up and with the Max floor applied as stored runs do."""
    out: dict[str, int | None] = {}
    for field in FIELDS:
        pairs = [
            (Decimal(getattr(d, field)), d.weight)
            for d in donors
            if getattr(d, field) is not None and d.weight > 0
        ]
        result = strategy(pairs)
        if isinstance(result, tuple):  # weighted_mean returns (mean, numerator, denominator)
            result = result[0]
        out[field] = None if result is None else _ceil(result)
    out["max_stock"], _ = apply_max_floor(out["rop"], out["safety_stock"], out["max_stock"])
    return out


def compare_target(
    material: str,
    plant: str,
    donors: Sequence[StoredDonor],
    ratio: Decimal = DEFAULT_MATERIAL_DIFFERENCE_RATIO,
) -> Comparison:
    """Both estimates for one target, from its stored donors."""
    mean = _estimate(donors, weighted_mean)
    median = _estimate(donors, weighted_median)
    material_difference = any(
        mean[f] is not None
        and median[f] is not None
        and mean[f] != median[f]
        and Decimal(abs(mean[f] - median[f])) >= ratio * mean[f]
        for f in FIELDS
    )
    return Comparison(material, plant, len(donors), mean, median, material_difference)


def summarise(comparisons: Iterable[Comparison]) -> dict[str, object]:
    """Counts a reader wants first: how many targets, how many moved, how many materially."""
    rows = list(comparisons)
    moved = [c for c in rows if c.rop_delta]
    return {
        "targets": len(rows),
        "rop_unchanged": len(rows) - len(moved),
        "rop_lower_with_median": sum(1 for c in moved if c.rop_delta < 0),
        "rop_higher_with_median": sum(1 for c in moved if c.rop_delta > 0),
        "material_difference": sum(1 for c in rows if c.material_difference),
        "largest_rop_reduction": min((c.rop_delta for c in moved), default=0),
    }


_SQL = """
    SELECT n.sap_material_number, n.sap_plant_code, n.combined_similarity,
           n.safety_stock, n.rop, n.max_stock
      FROM i7_oar_neighbour n
      JOIN i7_oar_target t
        ON t.oar_run_id = n.oar_run_id
       AND t.sap_material_number = n.sap_material_number
       AND t.sap_plant_code = n.sap_plant_code
     WHERE n.oar_run_id = :run_id
       AND t.estimate_status = 'SUCCESS'
       AND n.inventory_eligible = 1
     ORDER BY n.sap_material_number, n.sap_plant_code, n.rank
"""


def load(session, run_id: int) -> list[Comparison]:
    from sqlalchemy import text

    grouped: dict[tuple[str, str], list[StoredDonor]] = {}
    for row in session.execute(text(_SQL), {"run_id": run_id}):
        grouped.setdefault((row.sap_material_number, row.sap_plant_code), []).append(
            StoredDonor(
                weight=Decimal(str(row.combined_similarity)) if row.combined_similarity is not None else Decimal(0),
                safety_stock=row.safety_stock,
                rop=row.rop,
                max_stock=row.max_stock,
            )
        )
    return [compare_target(m, p, donors) for (m, p), donors in grouped.items()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", type=int, help="OAR run to read (default: the latest succeeded)")
    parser.add_argument("--top", type=int, default=20, help="how many of the largest ROP reductions to list")
    parser.add_argument("--csv", help="write every target's comparison to this file")
    args = parser.parse_args(argv)

    from sqlalchemy import text

    from app.core.db import get_sessionmaker

    with get_sessionmaker()() as session:
        run_id = args.run_id or session.execute(
            text("select max(id) from i7_oar_run where status = 'succeeded'")
        ).scalar()
        if run_id is None:
            print("no succeeded OAR run to compare", file=sys.stderr)
            return 1
        comparisons = load(session, run_id)

    print(f"OAR run {run_id}")
    for key, value in summarise(comparisons).items():
        print(f"  {key}: {value}")
    biggest = sorted((c for c in comparisons if c.rop_delta), key=lambda c: c.rop_delta)[: args.top]
    print(f"\nlargest ROP reductions with the median (top {args.top}):")
    for c in biggest:
        print(
            f"  {c.material}/{c.plant}  donors={c.donors}  ROP mean {c.mean['rop']} -> median "
            f"{c.median['rop']} ({c.rop_delta:+d})"
        )

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["material", "plant", "donors", *(f"{f}_{kind}" for f in FIELDS for kind in ("mean", "median")),
                             "rop_delta", "material_difference"])
            for c in comparisons:
                writer.writerow([c.material, c.plant, c.donors,
                                 *(c.mean[f] if kind == "mean" else c.median[f] for f in FIELDS for kind in ("mean", "median")),
                                 c.rop_delta, c.material_difference])
        print(f"\nwrote {len(comparisons)} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
