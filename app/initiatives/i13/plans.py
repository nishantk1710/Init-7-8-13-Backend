"""Reader for the platform ``ConsumptionPlan`` records WATCH/exceptions compare against.

Not SAP data -- ``consumption_plans.csv`` is platform-owned reference data
(``data-generator/generated/platform/``), keyed by reservation. I13 does not
generate or fabricate a plan when none exists; it reports ``NO_PLAN``.
"""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class ConsumptionPlan:
    plan_id: str
    session_id: str
    reservation_number: str
    reservation_item: str
    material: str
    plant: str
    requester: str
    purpose: str
    planned_quantity: Decimal
    planned_use_date: date | None
    status: str


def load_consumption_plans(data_dir: Path) -> list[ConsumptionPlan]:
    path = data_dir / "platform" / "consumption_plans.csv"
    if not path.exists():
        return []

    plans: list[ConsumptionPlan] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            planned_use_date = date.fromisoformat(row["planned_use_date"]) if row.get("planned_use_date") else None
            plans.append(
                ConsumptionPlan(
                    plan_id=row["plan_id"],
                    session_id=row["session_id"],
                    reservation_number=row["Rsnum"],
                    reservation_item=row["Rspos"],
                    material=row["Matnr"],
                    plant=row["Werks"],
                    requester=row["requester"],
                    purpose=row["purpose"],
                    planned_quantity=Decimal(row["planned_quantity"]) if row.get("planned_quantity") else Decimal("0"),
                    planned_use_date=planned_use_date,
                    status=row["status"],
                )
            )
    return plans
