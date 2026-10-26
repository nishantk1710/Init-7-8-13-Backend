"""Editable Overview KPI formulas: ``/api/v1/i7/kpi-formulas``.

``GET /active`` is what the Overview's information cards read; the rest serve the *KPI formulas* page.
Nothing here is authenticated beyond the ``X-Actor-Id`` placeholder every I07 write uses -- the actor is
stamped on each saved version so the history says who changed what, and role gating arrives with Entra ID.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.api.i7.deps import get_session
from app.initiatives.i7.kpi_formulas import service
from app.initiatives.i7.kpi_formulas.definitions import DEFAULTS, FIELDS, KPI_TITLES, VARIABLES
from app.initiatives.i7.kpi_formulas.example import build_examples
from app.initiatives.i7.kpi_formulas.presentation import formula_lines
from app.initiatives.i7.kpi_formulas.resolved import Formulas, FormulaValidationError, load_active
from app.schemas.i7.errors import ApiError, conflict
from app.schemas.i7.kpi_formulas import (
    ActiveFormulasOut,
    ActiveSettings,
    ApplyIn,
    FiguresOut,
    FormulaFieldOut,
    FormulaInput,
    FormulasPageOut,
    KpiOut,
    PreviewOut,
    StatusOut,
    VersionOut,
)

router = APIRouter(prefix="/kpi-formulas", tags=["i7-kpi-formulas"])


def _invalid(exc: FormulaValidationError) -> ApiError:
    first = next(iter(exc.errors.values()))
    return ApiError(422, "FORMULA_INVALID", first, {"fields": exc.errors})


def _active_view(session: Session, formulas: Formulas) -> ActiveFormulasOut:
    return ActiveFormulasOut(
        version=formulas.version,
        is_default=formulas.is_default,
        settings=ActiveSettings(window_days=formulas.window_days, lookback_days=formulas.lookback_days),
        formula_lines=formula_lines(formulas),
        examples=build_examples(session, formulas),  # type: ignore[arg-type]
    )


def _status(session: Session) -> StatusOut:
    pending = service.applying_version(session)
    if pending is not None:
        return StatusOut(
            state="applying",
            version=pending.version,
            message="Rebuilding the usage data for the new formulas. This can take a minute.",
        )
    latest = service.history(session, limit=1)
    if latest and latest[0].status == service.FAILED:
        return StatusOut(state="failed", version=latest[0].version, message=latest[0].error)
    return StatusOut(state="idle")


def _fields(formulas: Formulas) -> list[FormulaFieldOut]:
    return [
        FormulaFieldOut(
            key=f.key,
            kpi=f.kpi,
            label=f.label,
            kind=f.kind,
            help=f.help,
            default=f.default,
            value=formulas.values[f.key],
            allowed=[FormulaInput(name=n, label=VARIABLES[n]) for n in f.allowed],
            minimum=f.minimum,
            maximum=f.maximum,
            needs_rebuild=f.needs_rebuild,
        )
        for f in FIELDS
    ]


@router.get("/active", response_model=ActiveFormulasOut, summary="The formulas the Overview is using now")
def get_active(session: Annotated[Session, Depends(get_session)]) -> ActiveFormulasOut:
    return _active_view(session, load_active(session))


@router.get("", response_model=FormulasPageOut, summary="Everything the KPI formulas page needs")
def get_page(session: Annotated[Session, Depends(get_session)]) -> FormulasPageOut:
    formulas = load_active(session)
    return FormulasPageOut(
        active=_active_view(session, formulas),
        kpis=[KpiOut(key=k, title=t) for k, t in KPI_TITLES.items()],
        fields=_fields(formulas),
        status=_status(session),
        history=[VersionOut.model_validate(v, from_attributes=True) for v in service.history(session)],
    )


@router.get("/status", response_model=StatusOut, summary="Is a change still being applied?")
def get_status(session: Annotated[Session, Depends(get_session)]) -> StatusOut:
    return _status(session)


@router.post("/preview", response_model=PreviewOut, summary="What the portfolio would show under a draft")
def post_preview(body: ApplyIn, session: Annotated[Session, Depends(get_session)]) -> PreviewOut:
    try:
        draft, _active, draft_figures, current_figures, needs_rebuild = service.preview(session, dict(body.fields))
    except FormulaValidationError as exc:
        raise _invalid(exc) from exc
    return PreviewOut(
        current=_figures_out(current_figures),
        draft=_figures_out(draft_figures),
        window_needs_rebuild=needs_rebuild,
        formula_lines=formula_lines(draft),
    )


def _figures_out(f: service.Figures) -> FiguresOut:
    return FiguresOut(
        stockout_at_risk=f.stockout_at_risk,
        stockout_evaluated=f.stockout_evaluated,
        stockout_insufficient=f.stockout_insufficient,
        excess_count=f.excess_count,
        excess_opportunity=float(f.excess_opportunity) if f.excess_opportunity is not None else None,
        working_capital=float(f.working_capital) if f.working_capital is not None else None,
    )


@router.post("/apply", response_model=StatusOut, summary="Save a new version and apply it")
def post_apply(
    body: ApplyIn,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> StatusOut:
    return _apply(body.fields, body.note, background, session, actor)


@router.post("/reset", response_model=StatusOut, summary="Go back to the built-in formulas")
def post_reset(
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> StatusOut:
    return _apply(dict(DEFAULTS), "Reset to the built-in formulas", background, session, actor)


def _apply(
    fields: dict,
    note: str | None,
    background: BackgroundTasks,
    session: Session,
    actor: Actor,
) -> StatusOut:
    try:
        row = service.save_and_apply(session, dict(fields), actor.id, note)
    except FormulaValidationError as exc:
        raise _invalid(exc) from exc
    except service.ApplyInProgress as exc:
        raise conflict("APPLY_IN_PROGRESS", str(exc)) from exc
    if row.status == service.APPLYING:
        background.add_task(service.run_rebuild, row.id)
        return StatusOut(
            state="applying",
            version=row.version,
            message="Rebuilding the usage data for the new formulas. This can take a minute.",
        )
    return StatusOut(state="idle", version=row.version)
