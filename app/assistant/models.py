"""W7 -- the shared assistant spine, as tables.

One session table, not one per initiative
------------------------------------------
The BAdI hands the session ID to **one field on one reservation**. Two tables
means two ID namespaces, and when ``S7K2M4P8Q1`` comes back off a reservation
there is no way to know which one to look in -- short of prefixing the ID, which
spends characters out of a ten-character budget that has none to spare.

Initiative 13 had already reached the same conclusion from the other direction.
Its ACT engine treats the session as an initiative-neutral concept
(``MISSING_SESSION``, ``INVALID_SESSION``, ``SESSION_WITHOUT_PLAN``), and
``NoPlanReason``'s own docstring anticipates *"a future CAPTURE source that
tracks sessions independently of plans (e.g. a reservation-time chatbot session
store)"*. This is that store. ``flow`` says which conversation ran.

Append-only, and what that forces
----------------------------------
Every table here follows ``i8_attestation``: no UPDATE, no DELETE, enforced by a
Postgres trigger rather than by convention, because "the service layer does not
do that" is one careless session away from being untrue.

That has a consequence worth stating out loud, because it shaped the schema
rather than merely constraining it: **a session has no status column.** There is
nowhere to write "completed" later. So the session row holds only what is true
at the instant it is minted -- who, what, when, and the advice as served -- and
everything that happens afterwards is a row in ``assistant_turn``. Whether a
session was completed or abandoned is *derived* from its turns
(``app.assistant.session.outcome``), which means the audit trail and the status
can never disagree.

That is not a workaround. An abandoned session is explicitly useful -- "advice
given, not acted on" is a number both FRSs want -- and a mutable status column
would have let a later write erase the evidence that the advice was ever served.

The author is passed separately from the body
----------------------------------------------
``requester``, ``author``, ``captured_by`` and every timestamp are set by the
server from the caller, never read from the request body. A record whose author
is self-declared is not an audit record. Same rule as ``attestor`` on
``i8_attestation``, for the same reason.

Why the served advice is Text and not JSONB
--------------------------------------------
``app/models/base.py`` requires portable constructs: the deployed database is
Azure SQL, and a JSONB column is a rewrite rather than a swap. The assessment is
stored as a JSON **string** in a ``Text`` column. Nothing queries into it -- it
is evidence to be read back whole, months later, by somebody asking what the
requester was actually shown -- so there is nothing to gain from a queryable
document type and a portability cost to paying for one.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_id(prefix: str) -> str:
    """A UUID-backed public identifier, generated here rather than by the database.

    Same reasoning as ``new_attestation_id``: these are quoted in an audit trail,
    and a sequence both leaks how many records exist and lets two environments
    mint colliding ids for different records.
    """
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def new_turn_id() -> str:
    return _new_id("TRN")


def new_justification_id() -> str:
    return _new_id("JUS")


def new_plan_id() -> str:
    return _new_id("PLN")


def new_suggestion_id() -> str:
    return _new_id("QSG")


class AssistantSession(Base):
    """One invocation of the assistant. The spine of I08 FR-8 and I13 FR-4.

    The primary key **is** the session ID the requester types into SAP -- there
    is no second surrogate key. One namespace, one lookup, and the ID that
    appears in an audit trail is the same one a person read off a screen.
    """

    __tablename__ = "assistant_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    """The minted session ID -- ``app.assistant.ids.mint``. Ten characters
    today; the column is wider so that changing ``assistant_session_id_length``
    is a configuration change and not a migration on audit history."""

    # --- what the conversation was about ---------------------------------

    flow: Mapped[str] = mapped_column(String(8), index=True)
    """``i08`` or ``i13``. Never ``none`` -- a material with no flow gets no
    session at all, so that value cannot reach this table."""

    material_id: Mapped[str] = mapped_column(String(40), index=True)
    """**Normalised before storing** -- ruling 5.1, same as the attestation."""

    plant: Mapped[str] = mapped_column(String(8), index=True)

    requested_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 3), nullable=True
    )
    """What the requester was about to reserve when the assistant opened, where
    the caller supplied it. Optional: the BAdI pop-up may fire before a quantity
    is entered, and a defaulted zero would be indistinguishable from a real
    one."""

    # --- how it was routed (W7.1) ----------------------------------------
    #
    # The routing inputs, not just its answer. A requester shown the wrong flow
    # asks why, and MARC's MRP type may have changed by the time anybody looks.

    eighty_series: Mapped[bool] = mapped_column(Boolean)
    material_scope: Mapped[str] = mapped_column(String(16))
    mrp_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    routing_reason: Mapped[str] = mapped_column(Text)

    # --- who and when ----------------------------------------------------

    requester: Mapped[str] = mapped_column(String(128), index=True)
    """Server-set from the caller, never from the body. Every session today is
    ``UNAUTHENTICATED_LOCAL_USER`` because Entra is not wired in -- that is
    visible rather than hidden, which is the point."""

    origin: Mapped[str] = mapped_column(String(16))
    """``BADI`` or ``PLATFORM`` -- whether SAP opened this or somebody started
    it from the platform. Both FRSs permit the second while the BAdI is in
    transport, and the two must stay tellable apart in any adoption number."""

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    """Server-set, UTC."""

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When this session stops being current. **Advisory.** Expiry is reported
    and never used to invalidate a session retrospectively: the reservation is
    already saved in SAP and the platform cannot write back, so treating an
    expired-but-present ID as non-compliant would raise an exception nobody can
    ever clear. Open question 11."""

    # --- the advice as served --------------------------------------------

    assessment: Mapped[str] = mapped_column(Text)
    """The deterministic assessment, as JSON, exactly as the requester saw it.

    Both FRSs require keeping the advice as served, because benefit attribution
    reads it months later and asks what the platform actually said. Recomputing
    it then would answer a different question: the register moves, stock moves,
    and an answer regenerated in March is not the answer somebody acted on in
    September."""

    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The model-written sentence, where one was served. Null when narrative is
    off or the provider failed -- and a null here with a populated
    ``assessment`` is the honest record of a turn that degraded to deterministic
    text, not a gap."""

    narrative_prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    narrative_prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    narrative_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Prompt identity and deployment, carried so an AI-written sentence can be
    traced to the exact prompt version behind it. The provenance rule from
    ``app/core/prompts.py``: "the model said so" is not acceptable provenance."""

    __table_args__ = (
        # The FR-8 access pattern: every session for this part at this plant.
        Index("ix_assistant_session_material_plant", "material_id", "plant"),
    )

    def __repr__(self) -> str:
        return (
            f"<AssistantSession {self.id} {self.flow} {self.material_id}@{self.plant} "
            f"for {self.requester}>"
        )


class AssistantTurn(Base):
    """One question asked and the answer given. Append-only, ordered by sequence.

    This is the conversation, and it is what makes the flow server-driven rather
    than a script that exists twice. The backend decides the next step from the
    turns so far; the frontend renders whatever it is handed.
    """

    __tablename__ = "assistant_turn"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_turn_id)

    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("assistant_session.id"), index=True
    )

    sequence: Mapped[int] = mapped_column(Integer)
    """0-based position in the conversation. Unique per session -- the database
    says so, which is what stops two concurrent answers to the same question
    both being recorded as if they were a dialogue."""

    step_id: Mapped[str] = mapped_column(String(64))
    """Which step of the script this was -- ``i08_repair_challenge``,
    ``i13_capture_plan``. Stable across prompt wording changes, so a query for
    "how often did anybody override the suggestion" keeps working when the
    sentence is reworded."""

    step_kind: Mapped[str] = mapped_column(String(16))

    question: Mapped[str] = mapped_column(Text)
    """The question **as asked**, stored rather than looked up from the script.
    The script will change; what this person was asked will not."""

    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The answer as JSON. Null for a step that only told the requester
    something -- an unanswered informational turn is a real state, not a
    missing answer."""

    actor: Mapped[str] = mapped_column(String(128))

    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # Reading a conversation back is always "this session, in order", and
        # the uniqueness is the concurrency guard described above.
        Index("uq_assistant_turn_session_sequence", "session_id", "sequence", unique=True),
    )

    def __repr__(self) -> str:
        return f"<AssistantTurn {self.session_id}#{self.sequence} {self.step_id}>"


class Justification(Base):
    """Why somebody went ahead anyway. I08 FR-7 and I13 FR-7, one table.

    Both FRSs describe the same record -- a reason category from a controlled
    list plus free text -- so it is one table with a ``kind`` column rather than
    two tables that will drift. ``kind`` is what lines this up with ACT's
    ``QUANTITY_OVERRIDE`` exception type and its ``JUSTIFICATION_ADDED`` event.
    """

    __tablename__ = "justification"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=new_justification_id
    )

    session_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("assistant_session.id"), nullable=True, index=True
    )
    """Nullable: a justification can also be recorded against an ACT exception
    that no session produced -- the exception queue is fed by detection over
    reservations, not only by the chat."""

    exception_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    """The ACT exception this answers, where it answers one.

    Deliberately **not** a foreign key. ``i13_act_exception`` is rebuilt by a
    detection run keyed on a deterministic business id, and a constraint here
    would let this table dictate what detection may re-derive. The link is
    recorded; the coupling is not."""

    kind: Mapped[str] = mapped_column(String(32), index=True)
    """``NEW_ACQUISITION`` / ``QUANTITY_OVERRIDE`` / ``PLAN_BREACH`` /
    ``NO_PLAN``."""

    reason_category: Mapped[str] = mapped_column(String(64), index=True)
    """From ``assistant_justification_reason_categories``. Configuration and not
    an enum, because VZI has not supplied the vocabulary yet -- open question
    9 -- and an enum would need a migration when they do."""

    free_text: Mapped[str] = mapped_column(Text)
    """The part a human actually reads. Required: a category alone records that
    a box was ticked, not that anybody thought about it."""

    material_id: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(8), index=True)

    author: Mapped[str] = mapped_column(String(128), index=True)
    """Server-set from the caller."""

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<Justification {self.id} {self.kind}/{self.reason_category} by {self.author}>"


class ConsumptionPlanRecord(Base):
    """I13 FR-4 -- what the requester said they would do with the material.

    This is the record ``plans.py`` has only ever read from a CSV of 742
    fabricated rows. It is the first real one.

    The FRS-complete plan, read narrowly
    -------------------------------------
    The FRS asks for a planned consumption **window** and a cost centre or order
    "where known". Today's ACT detection reads a single ``planned_use_date`` and
    no cost centre at all, so capturing the full plan produces a record the
    current consumer does not fully read.

    That was a deliberate choice between three options, and this is option 2:
    **capture everything, read narrowly, widen the reader later.** Capturing only
    what ACT reads today would have been faster and would have under-captured
    against the FRS permanently -- and this table is append-only, so it could
    never have been backfilled. ``window_start`` is what ``plans.py`` projects as
    ``planned_use_date``, so detection behaviour is unchanged until somebody
    widens it deliberately, with its own tests.
    """

    __tablename__ = "consumption_plan"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_plan_id)

    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("assistant_session.id"), index=True
    )
    """Required. A plan captured outside a session has no traceable origin,
    which is the one thing FR-4 asks this record to have."""

    # --- the SAP documents, linked LATER ---------------------------------

    reservation_number: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    reservation_item: Mapped[str | None] = mapped_column(String(8), nullable=True)
    """Both null at capture, and that is the normal case rather than a gap.

    The assistant runs **while the reservation is being created** -- it does not
    exist in SAP yet, so it has no number to record. That is exactly why the
    requester types the session ID into SAP: the link is made afterwards, from
    the reservation back to the session, which is FR-8 and needs ``Bednr``
    exposed on ``ReservationItemSet`` (blocker B2). Until then these stay null
    and the plan is keyed on its session."""

    material: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(8), index=True)

    # --- what the FRS asks for -------------------------------------------

    purpose: Mapped[str] = mapped_column(Text)

    planned_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3))

    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    """The planned consumption **window** -- FR-2(b). FR-7 breaches on "window
    end plus grace", which is a different date from the single
    ``planned_use_date`` today's detection reads. Nullable because a requester
    may genuinely not know when the part will be used, and a fabricated window
    would produce a fabricated breach."""

    cost_centre: Mapped[str | None] = mapped_column(String(32), nullable=True)
    order_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """FR-2(b), "where known" -- so optional, and never inferred. No
    deterministic source (EKKN/AUFK) is loaded, which is also why
    ``i13_cost_centre_attribution_enabled`` is off by default."""

    status: Mapped[str] = mapped_column(String(16), index=True)
    """``ACTIVE``. The CSV's vocabulary, kept so the reader projects one shape.

    Append-only means a plan is never edited to ``CANCELLED`` in place; a
    superseding row would record that, the same way an attestation amendment
    does. Nothing captures that yet and nothing pretends to."""

    captured_by: Mapped[str] = mapped_column(String(128), index=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_consumption_plan_material_plant", "material", "plant"),
    )

    def __repr__(self) -> str:
        return f"<ConsumptionPlanRecord {self.id} {self.material}@{self.plant} qty={self.planned_quantity}>"


class QuantitySuggestionRecord(Base):
    """I13 FR-3 -- what we suggested, what they kept, and why.

    Needed to evidence benefit: "the assistant suggested 2 and the requester
    reserved 2" is the whole claim I13 makes, and it cannot be reconstructed
    from the reservation alone. It is also what ACT reads to raise
    ``QUANTITY_OVERRIDE``, whose ``QuantityDecisionRecord`` already carries a
    ``suggested_quantity`` field that has been ``None`` for every caller because
    the suggestion engine did not exist.
    """

    __tablename__ = "quantity_suggestion"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_suggestion_id)

    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("assistant_session.id"), index=True
    )

    material: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(8), index=True)

    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    """What the requester came in wanting."""

    suggested_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)
    """What the arithmetic produced, or **null where no suggestion was made**.

    Null is a real and common answer -- too little consumption history to
    average over. It is not zero, and a reader must never round it to one: "we
    suggest nothing" and "we suggest none" are opposite instructions."""

    accepted_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    """What the requester settled on after seeing the suggestion."""

    suggestion_reason: Mapped[str] = mapped_column(Text)
    """Why that number, in a sentence, including the configured values it was
    computed from. A suggestion whose basis cannot be recovered is one nobody
    can argue with -- and these are OUR defaults until VZI confirms them."""

    # The arithmetic's inputs, so the number can be re-derived exactly.
    stock_on_hand: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)
    open_po_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)
    average_monthly_consumption: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 3), nullable=True
    )
    months_of_cover: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)
    cover_ceiling_months: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    lookback_months: Mapped[int] = mapped_column(Integer)
    min_history_consumptions: Mapped[int] = mapped_column(Integer)
    consumption_count: Mapped[int] = mapped_column(Integer)

    suggested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    @property
    def is_override(self) -> bool:
        """Whether the requester kept more than was suggested.

        ``False`` when no suggestion was made -- there is nothing to override,
        and calling that an override would manufacture a compliance finding out
        of a data gap.
        """
        if self.suggested_quantity is None:
            return False
        return self.accepted_quantity > self.suggested_quantity

    def __repr__(self) -> str:
        return (
            f"<QuantitySuggestionRecord {self.id} {self.material}@{self.plant} "
            f"suggested={self.suggested_quantity} accepted={self.accepted_quantity}>"
        )
