"""Wire shapes for the editable Overview KPI formulas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FormulaInput(BaseModel):
    name: str
    label: str


class FormulaFieldOut(BaseModel):
    key: str
    kpi: str
    label: str
    kind: Literal["number", "condition", "setting"]
    help: str
    default: str | int
    value: str | int
    allowed: list[FormulaInput] = []
    minimum: int | None = None
    maximum: int | None = None
    needs_rebuild: bool = False


class KpiOut(BaseModel):
    key: str
    title: str


class ExampleInput(BaseModel):
    label: str
    value: str


class ExampleOut(BaseModel):
    kpi: str
    material: str
    plant: str
    inputs: list[ExampleInput]
    substitution: list[str]
    result: str
    at_risk: bool | None = None


class ActiveSettings(BaseModel):
    window_days: int
    lookback_days: int


class ActiveFormulasOut(BaseModel):
    """What the Overview's information cards are built from."""

    version: int | None
    is_default: bool
    settings: ActiveSettings
    formula_lines: dict[str, list[str]]
    examples: dict[str, ExampleOut | None]


class VersionOut(BaseModel):
    version: int
    status: str
    created_by: str
    created_at: datetime
    applied_at: datetime | None
    note: str | None
    error: str | None
    rebuild_needed: bool
    rows_built: int | None


class StatusOut(BaseModel):
    state: Literal["idle", "applying", "failed"]
    version: int | None = None
    message: str | None = None


class FormulasPageOut(BaseModel):
    active: ActiveFormulasOut
    kpis: list[KpiOut]
    fields: list[FormulaFieldOut]
    status: StatusOut
    history: list[VersionOut]


class ApplyIn(BaseModel):
    fields: dict[str, str | int] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=500)


class FiguresOut(BaseModel):
    stockout_at_risk: int
    stockout_evaluated: int
    stockout_insufficient: int
    excess_count: int
    excess_opportunity: float | None
    working_capital: float | None


class PreviewOut(BaseModel):
    current: FiguresOut
    draft: FiguresOut
    window_needs_rebuild: bool
    """True when the usage window changed: the draft's stockout figures only reflect it after Apply."""
    formula_lines: dict[str, list[str]]
