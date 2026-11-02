"""Shared, isolated fixtures for the automatic-generation and completed-quarter tests.

Everything here is an **in-memory SQLite database created per test**: the real report generator runs against it, and
nothing can reach the shared Azure SQL database -- the engine is private to the test, the tests never import
``get_sessionmaker`` un-patched, and the synthetic recommendations below exist only inside that engine. Historical
rows are never invented anywhere else.

SQLite holds only the tables the report reads or writes. The stockout measurement (which needs the whole usage / stock /
lead-time schema) is replaced by the counts it would return; what is under test is how the report treats it, not the
stockout rule, which has its own tests.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.stockout_risk.counts import StockoutRiskCounts
from app.models.i7_features import MaterialFeature
from app.models.i7_forecast import Forecast, ForecastBacktestPath, ForecastRun
from app.models.i7_kpi_snapshot import KpiSnapshot, KpiSnapshotLog
from app.models.i7_pipeline import PipelineRun
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, SapAdoptionResult
from app.models.i7_reporting import QuarterlyReportRecord
from app.models.i7_staging import StagedConsumption, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput
from app.models.ingestion import IngestionRun

UTC = timezone.utc

TABLES = (
    Recommendation, ApprovalLedgerEntry, SapAdoptionResult, StagedConsumption, StagedMaterial, StagedMaterialPlant,
    StagedStock, StagingRun, StockoutRiskInput, MaterialFeature, ForecastRun, ForecastBacktestPath, Forecast,
    KpiSnapshot, KpiSnapshotLog, QuarterlyReportRecord, PipelineRun, IngestionRun,
)

OCT, NOV, DEC = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)


class NoLock:
    """An always-granted lock, standing in for the SQL Server application lock."""

    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def acquire(self) -> bool:
        self.acquired += 1
        return True

    def release(self) -> None:
        self.released += 1


class RefusedLock(NoLock):
    """A lock another instance already holds."""

    def acquire(self) -> bool:
        self.acquired += 1
        return False


def make_factory() -> sessionmaker:
    """A fresh in-memory database with the report's tables, as a session factory shaped like the real one."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()  # the models declare some indexes twice; SQLite refuses that
    metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def stub_stockout(monkeypatch, as_of: date | None = None, *, at_risk=0, evaluated=0, insufficient=0) -> None:
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(
            at_risk=at_risk, evaluated=evaluated, insufficient_data=insufficient, as_of=as_of
        ),
    )


def at(year: int, month: int, day: int = 10, hour: int = 9) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=UTC)


def rec(material: str, generated_at: datetime, *, plant: str = "1300", run: int = 1, status: str = "READY_FOR_REVIEW",
        price: Decimal | None = Decimal("10"), cur_rop=2, rec_rop=12, cur_max=100, rec_max=60) -> Recommendation:
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}-{run}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=run, forecast_run_id=run, inventory_run_id=run, oar_run_id=run, policy_id="p", policy_version=1,
        formula_version="f", status=status, impact_status="X", generated_at=generated_at, unit_price=price,
        current_rop=cur_rop, recommended_rop=rec_rop, current_max_stock=cur_max, recommended_max_stock=rec_max,
    )


def consumed(material: str, period: date, *, issues: int = 2, plant: str = "1300") -> StagedConsumption:
    return StagedConsumption(
        sap_material_number=material, sap_plant_code=plant, period=period, quantity=Decimal("10"), unit_of_measure="EA",
        movement_count=issues, issue_count=issues, reversal_count=0, staging_run_id=1, source_table="n_mseg",
    )


def ledger_event(recommendation_id: str, action: str, when: datetime, *, actor: str = "u1",
                 new_status: str = "PENDING_APPROVAL") -> ApprovalLedgerEntry:
    return ApprovalLedgerEntry(
        recommendation_id=recommendation_id, recommendation_version=1, actor_id=actor, actor_role="End User", action=action,
        previous_status="READY_FOR_REVIEW", new_status=new_status, comment=None, timestamp=when,
    )


def seed_q4_2026(session: Session) -> None:
    """Three populated months of **synthetic** Q4 2026 activity -- for an isolated test database only.

    October, November and December each get their own recommendation run (different material-plants and statuses), staged
    consumption for the month, and dated approval events, so every monthly chart has something in each month.
    """
    session.add_all([
        # October: run 1
        rec("A", at(2026, 10, 10), run=1), rec("B", at(2026, 10, 10), run=1, status="PENDING_APPROVAL"),
        rec("C", at(2026, 10, 12), run=1),
        # November: run 2 (A is re-recommended; D is new)
        rec("A", at(2026, 11, 12), run=2, rec_rop=15), rec("D", at(2026, 11, 12), run=2, plant="1500"),
        # December: run 3
        rec("A", at(2026, 12, 8), run=3, rec_rop=18), rec("E", at(2026, 12, 9), run=3, status="APPROVED"),
    ])
    session.add_all([consumed(m, month) for month in (OCT, NOV, DEC) for m in ("A", "B", "C")])
    session.add_all([
        ledger_event("REC-B-1300-1", "SUBMIT", at(2026, 10, 20)),
        ledger_event("REC-E-1300-3", "APPROVE", at(2026, 12, 12), actor="u2", new_status="APPROVED"),
    ])
    session.commit()
