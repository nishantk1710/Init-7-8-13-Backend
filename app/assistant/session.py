"""Minting a session, assembling what it serves, and reading one back.

One invocation, one row, written once
--------------------------------------
A session is minted when somebody opens the assistant against a material that
has something to say about it, and the row is never touched again. Everything
that happens afterwards is a turn.

That means **the outcome is derived, not stored** (:func:`outcome`). There is no
status column to update, because the table is append-only and there would be
nowhere to write to. This is not a workaround: a mutable status is a second
source of truth sitting next to an immutable log, and the two can disagree. A
derived one cannot.

No scope, no session
--------------------
:func:`start` mints nothing when the router says the material is neither
80-series nor OAR. The caller gets a routing decision and no session, which is a
successful outcome and must not be reported as an error -- the assistant simply
has no opinion about a consumable, and recording that opinion once per
reservation would fill an append-only table with silence.

Where the numbers come from
----------------------------
Nothing here computes a business number. The I08 assessment is assembled from
the cached W5.1/W5.2 snapshot; the I13 assessment from W6.3's WATCH computation
and the cross-plant provider W6.6 already built. This module's contribution is
the session row, the ID, and putting the two halves behind one entry point.

**One caveat about "today", which is a live gap rather than a decision.**
``get_snapshot`` caches for the process lifetime, so its reference date freezes
at start-up. The assistant is the one screen that states an overdue date to a
person *at the moment they are deciding*, which makes it the most exposed
consumer of that cache. The overdue arithmetic here is done against the date
passed in rather than the snapshot's, so a long-running process reports the
right number of days -- but the underlying register rows are still as of the
cached build. Confirming the snapshot refresh is an open item.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.assistant import ids
from app.assistant import narrative as narrative_module
from app.assistant.models import AssistantSession, AssistantTurn
from app.assistant.narrative import write as write_narrative
from app.assistant.router import Flow, RoutedFlow, route
from app.assistant.script import next_step
from app.assistant.steps import Step
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.shared.plant_scope import IN_SCOPE_PLANTS, is_in_scope
from app.initiatives.i13.act_stock_provider import PostgresCrossPlantStockProvider
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.quantity import QuantitySuggestion, build_quantity_config, suggest
from app.initiatives.i13.reservation_assistant import build as build_i13_assessment
from app.initiatives.i13.watch import compute_watch_metrics
from app.initiatives.i8.reservation_assistant import build as build_i08_assessment
from app.initiatives.i8.service import get_snapshot
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository

logger = get_logger(__name__)

#: Who a session is issued to when nobody has authenticated. Named rather than
#: blank, and identical to the placeholder the rest of the platform uses, so
#: "we do not know who this was" is visible in the data instead of looking like
#: a real user id.
UNAUTHENTICATED = "UNAUTHENTICATED_LOCAL_USER"


class Origin(str, Enum):
    """How the assistant was opened."""

    BADI = "BADI"
    """SAP's reservation-entry pop-up."""

    PLATFORM = "PLATFORM"
    """Somebody started it from the platform. Both FRSs explicitly permit this
    while the BAdI is in transport, and adoption numbers have to be able to tell
    the two apart."""


class Outcome(str, Enum):
    """What became of a session. Derived from its turns -- never stored."""

    OPEN = "OPEN"
    """Still in progress, and not yet past its validity window."""

    COMPLETED = "COMPLETED"
    """Reached a terminal step."""

    ABANDONED = "ABANDONED"
    """Past its validity window without reaching a terminal step. **A useful
    number, not an error.** "Advice given, not acted on" is explicitly something
    both FRSs want counted, and a browser-driven conversation could not have
    counted it at all."""


class SessionError(ValueError):
    """A session cannot be started or advanced, and why."""


@dataclass(frozen=True)
class StartedSession:
    """What :func:`start` produced.

    ``session`` and ``step`` are ``None`` together, and only when the material
    is out of scope. That is a successful call.
    """

    routed: RoutedFlow
    session: AssistantSession | None
    step: Step | None
    assessment: object | None = None

    @property
    def minted(self) -> bool:
        return self.session is not None


def _assessment_for(
    db: DbSession,
    routed: RoutedFlow,
    *,
    requested_quantity: Decimal | None,
    today: date,
    settings: Settings,
    i13_config: I13Config,
):
    """Build the flow's assessment. One branch per flow, no shared shape.

    The two assessments are genuinely different objects -- one is about repairs,
    the other about cover -- and forcing them into a common interface would mean
    a lowest-common-denominator type that neither flow could say anything
    specific with.
    """
    if routed.flow is Flow.I08:
        snapshot = get_snapshot(db)
        return build_i08_assessment(
            material_id=routed.material_id,
            plant=routed.plant,
            universe_rows=snapshot.universe,
            repair_lines=snapshot.lines,
            today=today,
        )

    movement_repository = PostgresMovementRepository(db)
    # The I13 snapshot's WATCH row when there is one measured as of today: the
    # same numbers the WATCH screen shows, and no recompute on every turn (gap
    # G10). Otherwise the single-material live compute, as before. Never waits
    # for, or triggers, a snapshot build.
    from app.initiatives.i13.snapshot import peek_i13_snapshot

    snapshot = peek_i13_snapshot() if settings.i13_snapshot_enabled else None
    if snapshot is not None and snapshot.reference_date == today:
        metric = snapshot.watch.get((routed.material_id, routed.plant))
    else:
        metrics = compute_watch_metrics(
            movement_repository,
            PostgresProcurementRepository(db),
            PostgresReservationRepository(db),
            fetch_material_scope_index(db, material=routed.material_id, plant=routed.plant),
            i13_config,
            Path(settings.i13_data_dir),
            material=routed.material_id,
            plant=routed.plant,
            as_of=today,
            db=db,
        )
        metric = next(
            (
                m
                for m in metrics
                if m.material == routed.material_id and m.plant == routed.plant
            ),
            None,
        )
    if metric is None:
        # WATCH computes over materials with movement or ledger activity. A part
        # that is OAR but has never moved has no row, and there is genuinely
        # nothing to cross-check -- so this is refused rather than answered with
        # a row of zeros, which would read as "you have none and consume none"
        # when the truth is "we have never seen this part".
        raise SessionError(
            f"{routed.material_id} at plant {routed.plant} is OAR but has no WATCH "
            "row -- no movement or ledger activity has ever been recorded for it, "
            "so there is no stock or cover position to show. This is a data gap, "
            "not a zero."
        )

    return build_i13_assessment(
        material=routed.material_id,
        plant=routed.plant,
        metric=metric,
        cross_plant_stock=PostgresCrossPlantStockProvider(
            movement_repository
        ).get_other_plant_stock(material=routed.material_id, exclude_plant=routed.plant),
        requested_quantity=requested_quantity,
    )


def _stated(value: str | None) -> str | None:
    """A typed field, trimmed -- or ``None`` where nothing was typed.

    Whitespace is not an answer. A department of ``"  "`` would be stored as a
    value, count as one in the per-department adoption figure both FRSs ask for,
    and be indistinguishable on screen from a blank -- so it is collapsed to the
    NULL that already means "not stated". Append-only means this cannot be
    tidied up afterwards.
    """
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def suggestion_for(assessment, planned_quantity: Decimal) -> QuantitySuggestion:
    """FR-3's suggestion for an I13 assessment and a planned quantity.

    Lives here rather than in the script so the script stays pure, and so the
    same call serves the conversation and the standalone
    ``/api/i13/quantity-suggestion`` endpoint.
    """
    return suggest(assessment.metric, planned_quantity, build_quantity_config())


def _readable_facts(record: dict) -> str:
    """The assessment as flat ``name: value`` lines, for the prompt.

    Plain lines rather than JSON. The model is being asked to write one English
    sentence around numbers that are already decided, and handing it a nested
    document invites it to explore the structure instead -- which is how a
    narrative ends up quoting a field nobody meant to publish. Lists and nested
    objects are dropped for the same reason: the headline already carries
    whatever they contributed.
    """
    return "\n".join(
        f"{key}: {value}"
        for key, value in sorted(record.items())
        if value is not None
        and not isinstance(value, (list, dict))
        and key not in ("headline",)
    )


def start(
    db: DbSession,
    *,
    material_id: str,
    plant: str,
    requester: str | None = None,
    department: str | None = None,
    requested_for: str | None = None,
    origin: Origin = Origin.PLATFORM,
    requested_quantity: Decimal | None = None,
    today: date | None = None,
    settings: Settings | None = None,
    i13_config: I13Config | None = None,
) -> StartedSession:
    """Route a material, and mint a session if there is anything to say.

    Three names arrive here and they are not interchangeable.

    ``requester`` is **who operated the assistant**. It is passed separately from
    any request body by every caller -- a session whose owner is self-declared is
    not an audit record -- and defaults to the named placeholder rather than to
    blank.

    ``requested_for`` is **who wanted the part**, typed by the operator. It comes
    from the body precisely because it is not identity: it is a property of the
    reservation, like the material number. Passing it through ``requester`` would
    turn a typed name into an audit author, which is the one thing the split
    between these two columns exists to prevent.

    ``department`` is the requester's, not the operator's.

    ``requested_quantity`` is **no longer sent by the entry point** -- the
    quantity of record is captured inside the conversation against a purpose and
    a window. The argument stays because :mod:`app.assistant.turns` replays
    sessions minted before the field was dropped, and those carry a real value.
    """
    settings = settings or get_settings()
    i13_config = i13_config or get_i13_config()
    today = today or date.today()

    # Refused before routing, not after: a session is an append-only record, and
    # one opened for a plant the platform does not serve would be advice about
    # stock and repairs nothing here is allowed to count.
    if not is_in_scope(plant):
        raise SessionError(
            f"plant {plant!r} is outside the platform's scope: "
            f"{', '.join(IN_SCOPE_PLANTS)}"
        )

    routed = route(db, material_id, plant)
    if not routed.in_scope:
        logger.info(
            "Assistant not opened for %s at %s: %s",
            routed.material_id,
            routed.plant,
            routed.reason,
        )
        return StartedSession(routed=routed, session=None, step=None)

    assessment = _assessment_for(
        db,
        routed,
        requested_quantity=requested_quantity,
        today=today,
        settings=settings,
        i13_config=i13_config,
    )

    record = assessment.as_record(today)
    headline = record["headline"]

    # Optional, off by default, and it cannot fail the turn: the advice is
    # already complete by this point, and a provider outage must not cost the
    # requester their answer. See app/assistant/narrative.py for the deviation
    # this implements and why it needs sign-off.
    narrative = write_narrative(
        prompt_id=(
            narrative_module.I08_PROMPT
            if routed.flow is Flow.I08
            else narrative_module.I13_PROMPT
        ),
        headline=headline,
        facts=_readable_facts(record),
        settings=settings,
    )
    if not narrative.served and narrative.reason:
        logger.debug("No narrative for this session: %s", narrative.reason)

    issued_at = datetime.now(timezone.utc)
    session = AssistantSession(
        id=ids.mint(settings),
        flow=routed.flow.value,
        material_id=routed.material_id,
        plant=routed.plant,
        department=_stated(department),
        requested_for=_stated(requested_for),
        requested_quantity=requested_quantity,
        eighty_series=routed.eighty_series,
        material_scope=routed.material_scope.value,
        mrp_type=routed.mrp_type,
        routing_reason=routed.reason,
        requester=requester or UNAUTHENTICATED,
        origin=origin.value,
        expires_at=issued_at + timedelta(hours=settings.assistant_session_ttl_hours),
        # The advice AS SERVED. Recomputing this later would answer a different
        # question -- the register and the stock both move.
        assessment=json.dumps(record, sort_keys=True),
        narrative=narrative.text,
        narrative_prompt_id=narrative.prompt_id,
        narrative_prompt_version=narrative.prompt_version,
        narrative_model=narrative.model,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    step = next_step(
        flow=routed.flow,
        session_id=session.id,
        assessment=assessment,
        answers={},
        today=today,
        reason_categories=settings.assistant_justification_reason_category_list,
    )

    logger.info(
        "Assistant session %s minted: %s flow for %s at %s, for %s (%s), "
        "opened by %s (%s)",
        session.id,
        session.flow,
        session.material_id,
        session.plant,
        session.requested_for or "nobody named",
        session.department or "no department",
        session.requester,
        session.origin,
    )
    return StartedSession(
        routed=routed, session=session, step=step, assessment=assessment
    )


def load(db: DbSession, session_id: str, settings: Settings | None = None) -> AssistantSession:
    """One session by ID, or raise :class:`SessionError` saying which rule failed.

    The ID is parsed before it is looked up, so a mistyped one is reported as
    mistyped rather than as missing. Those are different answers: I13's
    ``INVALID_SESSION`` and ``MISSING_SESSION`` exist precisely to tell them
    apart, and a lookup that collapsed them would turn somebody's typo into a
    compliance finding.
    """
    settings = settings or get_settings()
    try:
        parsed = ids.parse(session_id, settings)
    except ids.SessionIdError as error:
        raise SessionError(str(error)) from error

    session = db.get(AssistantSession, parsed)
    if session is None:
        raise SessionError(
            f"No session {parsed}. The reference is well formed -- it passes its "
            "check character -- so this was either never issued or was issued in "
            "another environment."
        )
    return session


def turns(db: DbSession, session_id: str) -> list[AssistantTurn]:
    """Every turn of one conversation, in order."""
    return list(
        db.execute(
            select(AssistantTurn)
            .where(AssistantTurn.session_id == session_id)
            .order_by(AssistantTurn.sequence)
        ).scalars()
    )


def answers_of(turn_rows: list[AssistantTurn]) -> dict[str, dict]:
    """The answers so far, keyed by step id -- the script's entire input.

    Later turns win, which only matters if the same step were ever answered
    twice. The unique index on (session, sequence) makes that a deliberate act
    rather than a race, and taking the later one is the only defensible reading
    of an append-only log.
    """
    return {
        turn.step_id: json.loads(turn.answer)
        for turn in turn_rows
        if turn.answer is not None
    }


def outcome(
    session: AssistantSession,
    turn_rows: list[AssistantTurn],
    *,
    now: datetime | None = None,
) -> Outcome:
    """What became of this session.

    Derived, never stored -- see the module docstring. A session is COMPLETED
    the moment a terminal turn is recorded, OPEN until its window closes, and
    ABANDONED after that.
    """
    now = now or datetime.now(timezone.utc)

    if any(turn.step_kind == "terminal" for turn in turn_rows):
        return Outcome.COMPLETED

    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        # SQLite and some drivers hand back naive datetimes for a timestamptz
        # column. Treating a naive value as UTC matches how it was written.
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    return Outcome.OPEN if now <= expires_at else Outcome.ABANDONED


def is_expired(session: AssistantSession, *, now: datetime | None = None) -> bool:
    """Whether the validity window has closed.

    **Reported, never enforced.** An expired-but-present session ID on a
    reservation is not treated as non-compliant: the reservation is already
    saved in SAP and the platform cannot write back, so raising an exception
    against it would create a finding nobody can ever clear. Open question 11.
    """
    now = now or datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now > expires_at
