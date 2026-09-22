"""W7.4: persistence for quantity suggestions, and the bridge into W6.6.

The only module in this package that imports SQLAlchemy --
``quantity_suggestion.py`` stays pure, the same separation
``act_exception_store.py`` keeps for ACT. Three jobs:

1. **Issue a suggestion.** Read the W6.3 mart row for one (material, plant),
   run the pure engine over it, phrase the reason, insert one row. The mart
   is the *only* source of AMC/stock/open-PO/history here -- W7.4 never
   recomputes a consumption rate (see ``quantity_suggestion.py``).
2. **Record the outcome.** Acceptance (FRS §8) and override justifications,
   updated/appended in place. Never a delete-then-insert refresh; see
   ``app.models.i13_quantity_suggestion``'s docstring for why these rows
   cannot be regenerated.
3. **Feed W6.6.** ``build_quantity_decision_records`` turns decided
   suggestions into ``QuantityDecisionRecord``s. W6.6 built the
   quantity-override detection path and then left it idle, because
   ``suggested_quantity`` was ``None`` for every caller ("the suggestion
   engine is W7.4"). This is the wire that was missing.

**Only decided suggestions reach W6.6.** A row with ``accepted IS NULL`` is a
suggestion nobody has answered yet; raising an override exception against it
would mean flagging a requester for a decision they have not made. W6.6's
own reason text says "Requester-confirmed quantity differs from the system
suggestion", so confirmation is the trigger, and the quantity carried into
detection is the one the requester actually kept: the suggested figure where
they accepted, their own where they did not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.initiatives.i13.act.domain import QuantityDecisionRecord
from app.initiatives.i13.config import QuantitySuggestionConfig
from app.initiatives.i13.quantity_suggestion import (
    QuantitySuggestion,
    QuantitySuggestionInputs,
    SuggestionDirection,
    compute_quantity_suggestion,
)
from app.initiatives.i13.quantity_suggestion_reason import phrase_reason
from app.models.i13_quantity_suggestion import QuantityJustificationRecord, QuantitySuggestionRecord
from app.models.i13_watch_mart import WatchMetricMart

ZERO = Decimal("0")


class WatchMetricNotFoundError(LookupError):
    """No W6.3 mart row for this (material, plant).

    Deliberately not a silently-zeroed suggestion: an absent mart row means
    the WATCH refresh has not run for this material, not that it has no
    stock and no consumption. Guessing the latter would put a confident
    purchase figure on top of data that was never computed.
    """


def _inputs_from_mart(
    row: WatchMetricMart, *, requested_quantity: Decimal, plan_window_months: Decimal
) -> QuantitySuggestionInputs:
    return QuantitySuggestionInputs(
        material=row.material,
        plant=row.plant,
        requested_quantity=requested_quantity,
        plan_window_months=plan_window_months,
        average_monthly_consumption=row.average_monthly_consumption or ZERO,
        # stock_on_hand is nullable in the mart (no MARD row read for that
        # material-plant). Treated as zero here, which is the conservative
        # direction for this engine -- it makes the ceiling smaller and the
        # suggestion lower, never higher.
        stock_on_hand=row.stock_on_hand or ZERO,
        open_po_quantity=row.open_po_quantity or ZERO,
        consumption_count_12m=row.consumption_count_12m or 0,
    )


def _to_row(
    suggestion: QuantitySuggestion,
    *,
    suggestion_id: str,
    session_id: str | None,
    reservation_number: str | None,
    reservation_item: str | None,
    requester_id: str | None,
    reason_text: str,
    reason_source: str,
    reason_prompt_id: str | None,
    reason_prompt_version: int | None,
    reason_model: str | None,
) -> QuantitySuggestionRecord:
    return QuantitySuggestionRecord(
        suggestion_id=suggestion_id,
        session_id=session_id,
        material=suggestion.material,
        plant=suggestion.plant,
        reservation_number=reservation_number,
        reservation_item=reservation_item,
        requester_id=requester_id,
        requested_quantity=suggestion.requested_quantity,
        suggested_quantity=suggestion.suggested_quantity,
        direction=suggestion.direction.value,
        reason_code=suggestion.reason_code.value,
        reason_text=reason_text,
        reason_source=reason_source,
        reason_prompt_id=reason_prompt_id,
        reason_prompt_version=reason_prompt_version,
        reason_model=reason_model,
        average_monthly_consumption=suggestion.average_monthly_consumption,
        stock_on_hand=suggestion.stock_on_hand,
        open_po_quantity=suggestion.open_po_quantity,
        consumption_count_12m=suggestion.consumption_count_12m,
        plan_window_months=suggestion.plan_window_months,
        resulting_cover_months=suggestion.resulting_cover_months,
        plan_need_quantity=suggestion.plan_need_quantity,
        net_need_quantity=suggestion.net_need_quantity,
        ceiling_quantity=suggestion.ceiling_quantity,
        cover_ceiling_months=suggestion.cover_ceiling_months,
        minimum_history_count=suggestion.minimum_history_count,
        lookback_months=suggestion.lookback_months,
        accepted=None,
        calculated_at=suggestion.calculated_at,
    )


def issue_quantity_suggestion(
    session: Session,
    config: QuantitySuggestionConfig,
    *,
    material: str,
    plant: str,
    requested_quantity: Decimal,
    plan_window_months: Decimal,
    as_of: datetime,
    session_id: str | None = None,
    reservation_number: str | None = None,
    reservation_item: str | None = None,
    requester_id: str | None = None,
    settings: Settings | None = None,
) -> QuantitySuggestionRecord:
    """Compute one suggestion from the W6.3 mart and persist it.

    The caller owns the transaction boundary (this codebase's convention):
    this function flushes so the row is readable back, and does not commit.

    Raises ``WatchMetricNotFoundError`` when W6.3 has no row for this
    material-plant.
    """
    row = session.get(WatchMetricMart, (material, plant))
    if row is None:
        raise WatchMetricNotFoundError(f"no W6.3 WATCH mart row for {material}/{plant}")

    suggestion = compute_quantity_suggestion(
        _inputs_from_mart(row, requested_quantity=requested_quantity, plan_window_months=plan_window_months),
        config,
        as_of=as_of,
    )
    phrasing = phrase_reason(suggestion, settings=settings)

    record = _to_row(
        suggestion,
        suggestion_id=uuid.uuid4().hex,
        session_id=session_id,
        reservation_number=reservation_number,
        reservation_item=reservation_item,
        requester_id=requester_id,
        reason_text=phrasing.text,
        reason_source=phrasing.source,
        reason_prompt_id=phrasing.prompt_id,
        reason_prompt_version=phrasing.prompt_version,
        reason_model=phrasing.model,
    )
    session.add(record)
    session.flush()
    return record


def get_quantity_suggestion(session: Session, suggestion_id: str) -> QuantitySuggestionRecord | None:
    return session.get(QuantitySuggestionRecord, suggestion_id)


def list_quantity_suggestions(
    session: Session,
    *,
    material: str | None = None,
    plant: str | None = None,
    session_id: str | None = None,
    reservation_number: str | None = None,
    direction: str | None = None,
    reason_code: str | None = None,
    accepted: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[QuantitySuggestionRecord]:
    """Paginated listing. Paginated in SQL, not after the fact: several
    existing I13 list endpoints slice an already-materialised list, which
    still loads every row first. This one does not."""
    stmt = select(QuantitySuggestionRecord)
    if material:
        stmt = stmt.where(QuantitySuggestionRecord.material == material)
    if plant:
        stmt = stmt.where(QuantitySuggestionRecord.plant == plant)
    if session_id:
        stmt = stmt.where(QuantitySuggestionRecord.session_id == session_id)
    if reservation_number:
        stmt = stmt.where(QuantitySuggestionRecord.reservation_number == reservation_number)
    if direction:
        stmt = stmt.where(QuantitySuggestionRecord.direction == direction.upper())
    if reason_code:
        stmt = stmt.where(QuantitySuggestionRecord.reason_code == reason_code.upper())
    if accepted is not None:
        stmt = stmt.where(QuantitySuggestionRecord.accepted == accepted)
    stmt = stmt.order_by(
        QuantitySuggestionRecord.calculated_at.desc(), QuantitySuggestionRecord.suggestion_id
    ).limit(limit).offset(offset)
    return list(session.execute(stmt).scalars().all())


def record_acceptance(
    session: Session,
    suggestion_id: str,
    *,
    accepted: bool,
    actor_id: str,
    as_of: datetime,
) -> QuantitySuggestionRecord:
    """Record whether the requester took the suggestion (FRS §8).

    Raises ``LookupError`` for an unknown suggestion and ``ValueError`` where
    the engine never made one -- there is nothing to accept when the answer
    was NO_SUGGESTION, and storing ``accepted=True`` against it would put a
    benefit claim behind a figure that does not exist.
    """
    record = session.get(QuantitySuggestionRecord, suggestion_id)
    if record is None:
        raise LookupError(f"quantity suggestion {suggestion_id} not found")
    if record.suggested_quantity is None:
        raise ValueError(
            f"quantity suggestion {suggestion_id} made no suggestion ({record.reason_code}); "
            "there is nothing to accept or reject"
        )

    record.accepted = accepted
    record.accepted_at = as_of
    record.accepted_by = actor_id
    session.flush()
    return record


def add_justification(
    session: Session,
    suggestion_id: str,
    *,
    reason_category: str,
    free_text: str,
    actor_id: str,
    as_of: datetime,
) -> QuantityJustificationRecord:
    """Append the requester's justification for keeping a different quantity.

    Raises ``LookupError`` for an unknown suggestion. Deliberately does NOT
    require ``accepted`` to have been recorded first: a requester typing
    their reason before pressing the button is the normal order of events,
    and refusing the words because the flag is not set yet would lose them.
    """
    if session.get(QuantitySuggestionRecord, suggestion_id) is None:
        raise LookupError(f"quantity suggestion {suggestion_id} not found")

    justification = QuantityJustificationRecord(
        suggestion_id=suggestion_id,
        reason_category=reason_category,
        free_text=free_text,
        actor_id=actor_id,
        created_at=as_of,
    )
    session.add(justification)
    session.flush()
    return justification


def list_justifications(session: Session, suggestion_id: str) -> list[QuantityJustificationRecord]:
    """Every justification for one suggestion, oldest first. Append-only, so
    the last row is the current reasoning and the earlier ones are history."""
    stmt = (
        select(QuantityJustificationRecord)
        .where(QuantityJustificationRecord.suggestion_id == suggestion_id)
        .order_by(QuantityJustificationRecord.created_at, QuantityJustificationRecord.justification_id)
    )
    return list(session.execute(stmt).scalars().all())


def build_quantity_decision_records(
    session: Session,
    *,
    material: str | None = None,
    plant: str | None = None,
) -> list[QuantityDecisionRecord]:
    """Decided suggestions -> W6.6 ``QuantityDecisionRecord``s.

    This is what makes W6.6's QUANTITY_OVERRIDE detection fire for the first
    time. ``requested_quantity`` on the record is the quantity the requester
    **kept**, so ``detect_quantity_override``'s ``requested != suggested``
    comparison means what its name says:

    * accepted -> they took the suggested figure -> no override, and any
      previously-raised exception for that key resolves itself on the next
      detection run;
    * not accepted -> they kept their own figure -> override, with the
      variance and their justification carried as evidence.

    Undecided rows (``accepted IS NULL``) and declined suggestions
    (``NO_SUGGESTION``) are excluded -- see the module docstring.
    """
    stmt = select(QuantitySuggestionRecord).where(
        QuantitySuggestionRecord.accepted.is_not(None),
        QuantitySuggestionRecord.suggested_quantity.is_not(None),
        QuantitySuggestionRecord.direction != SuggestionDirection.NO_SUGGESTION.value,
    )
    if material:
        stmt = stmt.where(QuantitySuggestionRecord.material == material)
    if plant:
        stmt = stmt.where(QuantitySuggestionRecord.plant == plant)
    # Oldest first, and that ordering is load-bearing: two suggestions against
    # the same reservation share a business key (quantity_override_business_key)
    # and therefore one exception, so the LAST record detection sees must be
    # the most recent decision -- otherwise a superseded answer would win.
    stmt = stmt.order_by(QuantitySuggestionRecord.calculated_at, QuantitySuggestionRecord.suggestion_id)
    rows = list(session.execute(stmt).scalars().all())

    # One query for every justification in scope rather than one per row:
    # this runs inside W6.6's detection route, which already does real work.
    latest_justification: dict[str, QuantityJustificationRecord] = {}
    if rows:
        justification_stmt = (
            select(QuantityJustificationRecord)
            .where(QuantityJustificationRecord.suggestion_id.in_([row.suggestion_id for row in rows]))
            .order_by(QuantityJustificationRecord.created_at, QuantityJustificationRecord.justification_id)
        )
        # Ordered oldest-first, so the last write per suggestion wins -- the
        # current reasoning, with the earlier ones still on record.
        for justification in session.execute(justification_stmt).scalars().all():
            latest_justification[justification.suggestion_id] = justification

    records: list[QuantityDecisionRecord] = []
    for row in rows:
        latest = latest_justification.get(row.suggestion_id)
        kept_quantity = row.suggested_quantity if row.accepted else row.requested_quantity
        records.append(
            QuantityDecisionRecord(
                material=row.material,
                plant=row.plant,
                reservation_number=row.reservation_number,
                reservation_item=row.reservation_item,
                session_id=row.session_id,
                requester_id=row.requester_id,
                requested_quantity=kept_quantity,
                suggested_quantity=row.suggested_quantity,
                suggestion_reason=row.reason_code,
                override_justification=(
                    f"{latest.reason_category}: {latest.free_text}" if latest is not None else None
                ),
            )
        )
    return records
