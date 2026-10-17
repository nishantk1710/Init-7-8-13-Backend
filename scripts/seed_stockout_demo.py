"""Seed a LOCAL database so the High Stockout Risk card has something to show.

    python -m scripts.seed_stockout_demo            # add (or refresh) the demo rows
    python -m scripts.seed_stockout_demo --clear    # remove them again

It writes a handful of ``DEMO-`` material-plants straight into the tables the card reads --
recommendations, staged stock, the feature store and ``i7_stockout_risk_input`` -- one for each
case the rule distinguishes, so the card, its "As of" date and its counts can be checked without the
SAP extract. The rows are dated as of 2026-05-31, the default ``I7_STOCKOUT_RISK_AS_OF``.

It REFUSES to run unless DATABASE_URL points at this machine (localhost / 127.0.0.1): these are
made-up rows and must never reach Azure SQL. See the module's end for the numbers the card should
show.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.stockout_risk import CONSUMPTION_WINDOW_DAYS
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedStock
from app.models.i7_stockout_risk import StockoutRiskInput

AS_OF = date(2026, 5, 31)
PREFIX = "DEMO-"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Every case needs 900 / 90 x 10 = 100 units to cover its lead time, unless noted.
#   code, plant, what it shows, days since last issue (None = never issued), consumption over the
#   window, lead time (None = missing), stock on hand (None = no stock row), has a recommendation
CASES = [
    ("RISK", "1300", "Fast, 50 on hand against 100 needed: AT RISK", 30, 900, 10, 50, True),
    ("SAFE", "1300", "Fast, 500 on hand: not at risk", 30, 900, 10, 500, True),
    ("EXACT", "1300", "Fast, exactly 100 on hand: not at risk (strictly below)", 30, 900, 10, 100, True),
    ("SLOW-RISK", "1300", "Slow (500 days since last issue), 10 on hand: AT RISK", 500, 900, 10, 10, True),
    ("ZERO-USE", "1300", "Fast, nothing issued in the last 90 days: not at risk", 120, 0, 10, 0, True),
    ("NO-LEAD", "1300", "Fast, no lead time: INSUFFICIENT DATA", 30, 900, None, 0, True),
    ("NO-STOCK", "1300", "Fast, no stock row: INSUFFICIENT DATA", 30, 900, 10, None, True),
    ("NON-MOVING", "1300", "Last issue 800 days ago: Non-moving, outside the KPI", 800, 900, 10, 0, True),
    ("NEVER", "1300", "Never issued (no usage row): Non-moving, outside the KPI", None, 0, 10, 0, True),
    ("SMOOTH", "1300", "Smooth: no recommendation, counted from the feature store: AT RISK", 30, 900, 10, 20, False),
    ("NO-DATA-PLANT", "1500", "Plant with no movement data at all: INSUFFICIENT DATA", None, 0, 10, 0, True),
]

EXPECTED = {"at_risk": 3, "evaluated": 6, "insufficient_data": 3}


def refuse_unless_local() -> None:
    url = get_settings().database_url
    host = make_url(url).host if url else None
    if host not in LOCAL_HOSTS:
        sys.exit(
            f"Refusing to seed: DATABASE_URL points at {host!r}, not this machine. These are made-up "
            "rows. Point DATABASE_URL at a local SQL Server (127.0.0.1) first."
        )


def clear(session) -> None:
    for model in (Recommendation, StagedStock, MaterialFeature, StockoutRiskInput):
        session.execute(delete(model).where(model.sap_material_number.like(f"{PREFIX}%")))


def seed(session) -> None:
    clear(session)
    for code, plant, _what, days_ago, consumption, lead_time, on_hand, recommended in CASES:
        material = f"{PREFIX}{code}"
        lead = None if lead_time is None else Decimal(lead_time)

        if recommended:
            session.add(
                Recommendation(
                    recommendation_id=f"REC-{material}-{plant}",
                    sap_material_number=material,
                    sap_plant_code=plant,
                    feature_run_id=1,
                    forecast_run_id=1,
                    inventory_run_id=1,
                    oar_run_id=1,
                    policy_id="demo",
                    policy_version=1,
                    formula_version="demo",
                    status="READY_FOR_REVIEW",
                    impact_status="NOT_AVAILABLE",
                    is_oar=False,
                    demand_class="LUMPY",
                    lead_time_days=lead,
                    generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
                )
            )
        else:
            # A Smooth material-plant has no recommendation: the feature store supplies its class
            # and lead time.
            session.add(
                MaterialFeature(
                    sap_material_number=material,
                    sap_plant_code=plant,
                    history_status="SUFFICIENT",
                    data_sufficiency="LIMITED",
                    adi_status="OK",
                    cv_squared_status="OK",
                    oar_scope="OUT_OF_SCOPE",
                    feature_run_id=1,
                    demand_class="SMOOTH",
                    lead_time_days=lead,
                )
            )

        if on_hand is not None:
            session.add(
                StagedStock(
                    sap_material_number=material,
                    sap_plant_code=plant,
                    storage_location="0001",
                    unrestricted_use_stock=Decimal(on_hand),
                    source_table="demo",
                    staging_run_id=1,
                )
            )

        # Plant 1500 and the "never issued" material have no usage row, which is how the card tells
        # "no movement data for this plant" from "this material was never issued".
        if days_ago is not None and plant == "1300":
            session.add(
                StockoutRiskInput(
                    sap_material_number=material,
                    sap_plant_code=plant,
                    as_of_date=AS_OF,
                    last_issue_date=AS_OF - timedelta(days=days_ago),
                    consumption_90d=Decimal(consumption),
                    window_days=CONSUMPTION_WINDOW_DAYS,
                )
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed (or clear) demo rows for the High Stockout Risk card.")
    parser.add_argument("--clear", action="store_true", help="Remove the DEMO- rows instead of adding them.")
    args = parser.parse_args()
    refuse_unless_local()

    with get_sessionmaker()() as session:
        if args.clear:
            clear(session)
            session.commit()
            print("Removed the DEMO- rows.")
            return 0
        seed(session)
        session.commit()

    print(f"Seeded {len(CASES)} DEMO- material-plants as of {AS_OF}:")
    for code, plant, what, *_ in CASES:
        print(f"  {PREFIX}{code:<14} {plant}  {what}")
    print(
        f"\nOn an otherwise empty database the card should read {EXPECTED['at_risk']} at risk, "
        f"{EXPECTED['evaluated']} evaluated ({EXPECTED['at_risk'] / EXPECTED['evaluated']:.0%}), "
        f"{EXPECTED['insufficient_data']} need data, 'As of 31 May 2026'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
