"""Saving and reading summary reports. Insert-only: see the model's docstring.

The caller owns the transaction (this codebase's convention): ``save`` flushes,
the route commits.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.initiatives.i13.report.narrative import Narrative
from app.models.i13_summary_report import I13SummaryReport


def save(
    db: Session,
    *,
    facts: dict[str, Any],
    narrative: Narrative,
    generated_by: str,
    plant: str | None,
) -> I13SummaryReport:
    row = I13SummaryReport(
        id=uuid.uuid4().hex,
        generated_at=datetime.now(timezone.utc),
        generated_by=generated_by[:120],
        plant=plant,
        snapshot_version=facts.get("snapshot_version"),
        data_as_of=date.fromisoformat(facts["data_as_of"]) if facts.get("data_as_of") else None,
        headline=narrative.headline[:600],
        narrative_source=narrative.source,
        model=narrative.model,
        prompt_id=narrative.prompt_id,
        prompt_version=narrative.prompt_version,
        report_json=json.dumps({"facts": facts, "narrative": narrative.to_dict()}, default=str),
    )
    db.add(row)
    db.flush()
    return row


def list_reports(db: Session, *, plant: str | None = None, limit: int = 20) -> list[I13SummaryReport]:
    stmt = select(I13SummaryReport).order_by(I13SummaryReport.generated_at.desc()).limit(limit)
    if plant:
        stmt = stmt.where(I13SummaryReport.plant == plant)
    return list(db.execute(stmt).scalars().all())


def get_report(db: Session, report_id: str) -> I13SummaryReport | None:
    return db.get(I13SummaryReport, report_id)


def payload(row: I13SummaryReport) -> dict[str, Any]:
    """``{"facts": ..., "narrative": ...}`` as saved."""
    return json.loads(row.report_json)
