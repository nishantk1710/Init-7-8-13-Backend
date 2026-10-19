"""Where is the I07 pipeline? Which stage, how many done, how many left.

    python -m scripts.i7_progress                   # one report
    python -m scripts.i7_progress --watch 60        # repeat every 60 s (Ctrl+C to stop)
    python -m scripts.i7_progress --log /home/i7_pipeline_run.log

Read-only. Everything is read from the tables the stages write, plus the tail of
the pipeline's log for the one stage that saves only at the end:

* staging         5 steps, each saved on its own -> "n of 5 steps saved"
* features        one transaction -> shows nothing until it finishes (~4 min)
* forecasting     saved every 50 materials -> "done of total"
* inventory       one transaction -> shows nothing until it finishes (~1 min)
* OAR             results written in batches -> "written of targets"
* recommendations saved all at once at the end -> the log's "N of M explanations"

Nothing here computes a business figure: each number is a row count of something
a stage already wrote.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.contracts.enums import ScopeDecision
from app.initiatives.i7.smooth_demand import SMOOTH_DEMAND_CLASS
from app.models.i7_features import FeatureBuildRun, MaterialFeature
from app.models.i7_forecast import Forecast, ForecastRun
from app.models.i7_inventory import InventoryCalculation, InventoryRun
from app.models.i7_oar import OarRun, OarTargetResult
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagingRun
from app.initiatives.i7.recommendations.types import RECOMMENDATION_FORMULA_VERSION

DEFAULT_LOG = "/home/i7_pipeline_run.log"

# _FLAGGED: ``column == True``, not ``column.is_(True)``. SQL Server has no ``IS TRUE`` and
# rejects the ``IS 1`` that ``.is_(True)`` renders for a bit column; ``== True`` renders
# ``= 1`` there and is just as correct elsewhere.
STAGING_STEPS = ("materials_staged", "material_plants_staged", "stock_staged", "consumption_staged", "purchase_orders_staged")


def _latest(session: Session, model):
    return session.execute(select(model).order_by(model.id.desc()).limit(1)).scalars().first()


def _count(session: Session, statement) -> int:
    return int(session.execute(statement).scalar() or 0)


def _elapsed(run) -> str:
    if run is None or run.started_at is None:
        return ""
    started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
    end = run.finished_at or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    minutes = int((end - started).total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _state(run) -> str:
    return "not started" if run is None else run.status.upper()


def _process_running() -> bool:
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any("run_full_pipeline" in line and "grep" not in line for line in out.splitlines())


def _last_explanation_line(log_path: str) -> str | None:
    path = Path(log_path)
    if not path.exists():
        return None
    try:
        tail = path.read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        return None
    matches = re.findall(r"recommendation explanations: (\d+) of (\d+) done \(([^)]*)\)", tail)
    if not matches:
        return None
    done, total, sources = matches[-1]
    return f"{int(done):,} of {int(total):,} explanations written ({sources})"


def collect(session: Session, log_path: str = DEFAULT_LOG) -> dict:
    staging, features = _latest(session, StagingRun), _latest(session, FeatureBuildRun)
    forecast, inventory = _latest(session, ForecastRun), _latest(session, InventoryRun)
    oar = _latest(session, OarRun)
    rows: list[dict] = []

    steps = sum(1 for column in STAGING_STEPS if staging is not None and getattr(staging, column)) if staging else 0
    rows.append({"stage": "1 Staging", "run": staging, "detail": f"{steps} of {len(STAGING_STEPS)} steps saved"
                 + (f" (materials {staging.materials_staged:,}, consumption {staging.consumption_staged:,}, "
                    f"purchase orders {staging.purchase_orders_staged:,})" if staging and steps else "")})

    built = _count(session, select(func.count()).select_from(MaterialFeature))
    rows.append({"stage": "2 Features", "run": features,
                 "detail": f"{built:,} material-plants built" if built else "saved at the end of the stage"})

    detail = "saved every 50 materials"
    if forecast is not None and features is not None:
        total = _count(session, select(func.count()).select_from(MaterialFeature).where(
            MaterialFeature.history_status == "SUFFICIENT",
            MaterialFeature.demand_class != SMOOTH_DEMAND_CLASS,
            MaterialFeature.oar_scope != ScopeDecision.IN_SCOPE.value,
            MaterialFeature.baseline_model.is_not(None),
        ))
        done = _count(session, select(func.count(func.distinct(Forecast.sap_material_number + "/" + Forecast.sap_plant_code)))
                      .where(Forecast.forecast_run_id == forecast.id))
        detail = f"{done:,} of {total:,} material-plants forecast" + (f" ({100 * done // total}%, {total - done:,} left)" if total else "")
    rows.append({"stage": "3 Forecasting", "run": forecast, "detail": detail})

    calculations = _count(session, select(func.count()).select_from(InventoryCalculation))
    rows.append({"stage": "4 Inventory", "run": inventory,
                 "detail": f"{calculations:,} calculations" if calculations else "saved at the end of the stage"})

    detail = "results written in batches"
    if oar is not None:
        targets = _count(session, select(func.count()).select_from(MaterialFeature).where(
            MaterialFeature.oar_scope == ScopeDecision.IN_SCOPE.value))
        written = _count(session, select(func.count()).select_from(OarTargetResult).where(OarTargetResult.oar_run_id == oar.id))
        detail = f"{written:,} of {targets:,} OAR material-plants written" + (f" ({targets - written:,} left)" if targets else "")
    rows.append({"stage": "5 OAR similarity", "run": oar, "detail": detail})

    saved = _count(session, select(func.count()).select_from(Recommendation).where(
        Recommendation.formula_version == RECOMMENDATION_FORMULA_VERSION))
    note = _last_explanation_line(log_path)
    rows.append({"stage": "6 Recommendations", "run": None,
                 "detail": f"{saved:,} saved" if saved else (note or "saved all at once at the end"),
                 "state": "SUCCEEDED" if saved else ("RUNNING" if note else "not started")})

    deleted = None
    if built:
        deleted = {
            "plant": _count(session, select(func.count()).select_from(MaterialFeature).join(
                StagedMaterialPlant,
                (StagedMaterialPlant.sap_material_number == MaterialFeature.sap_material_number)
                & (StagedMaterialPlant.sap_plant_code == MaterialFeature.sap_plant_code),
            ).where(StagedMaterialPlant.deletion_flag == True)),  # noqa: E712 -- see _FLAGGED
            "material": _count(session, select(func.count()).select_from(MaterialFeature).join(
                StagedMaterial, StagedMaterial.sap_material_number == MaterialFeature.sap_material_number,
            ).where(StagedMaterial.deletion_flag == True)),  # noqa: E712 -- see _FLAGGED
        }
    return {"rows": rows, "deleted": deleted, "alive": _process_running()}


def render(report: dict) -> str:
    lines = [f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC   pipeline process: "
             f"{'RUNNING' if report['alive'] else 'NOT RUNNING'}", ""]
    current = None
    for row in report["rows"]:
        state = row.get("state") or _state(row["run"])
        if state == "RUNNING" and current is None:
            current = row["stage"]
        elapsed = _elapsed(row["run"])
        lines.append(f"  {row['stage']:<18} {state:<12} {elapsed:<8} {row['detail']}")
    lines.append("")
    lines.append(f"  NOW: {current}" if current else "  NOW: no stage is running")
    if report["deleted"] is not None:
        lines.append("")
        lines.append("  Flagged for deletion (LVORM = X) still in the run -- both must be 0:")
        lines.append(f"    plant level (MARC):    {report['deleted']['plant']:,}")
        lines.append(f"    material level (MARA): {report['deleted']['material']:,}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Show where the I07 pipeline is.")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="repeat every N seconds")
    parser.add_argument("--log", default=DEFAULT_LOG, help="the pipeline's log file")
    arguments = parser.parse_args()
    factory = get_sessionmaker()
    while True:
        with factory() as session:
            print(render(collect(session, arguments.log)), flush=True)
        if arguments.watch <= 0:
            return 0
        print("\n" + "-" * 60, flush=True)
        try:
            time.sleep(arguments.watch)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
