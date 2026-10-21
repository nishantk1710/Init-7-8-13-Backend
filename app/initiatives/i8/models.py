"""W5.3 -- the condition-to-repair attestation table.

This is the **first thing Initiative 08 writes**. Everything in W5.1 and W5.2 is
a read model over the July extract; this is a record the platform creates and
owns, because SAP has nowhere to put it.

Why the table lives here and not in ``app/models/``
---------------------------------------------------
``app/models/`` holds the raw SAP landing tables, which all three initiatives
read. Its own scope note draws the line: *"Tables derived from those, and
anything specific to one initiative, belong to that initiative."* An attestation
is specific to I08 -- nobody else records one -- so it belongs in the I08
package. It is imported from ``app/models/__init__.py`` anyway, because Alembic
autogenerate compares the database against ``Base.metadata`` and a model that
nothing imports is silently absent from it.

Immutability, and why it is the whole point
-------------------------------------------
**Nothing here is ever UPDATEd.** An attestation is a record of what a named
person judged at a named moment. Editing it in place does not correct history,
it destroys it -- and the reason this record exists at all is that today nobody
can tell a considered decision from a reflex.

So an amendment is a **new row** whose :attr:`RepairAttestation.supersedes`
points at the row it replaces. The original stays readable forever. That gives a
chain rather than a value, and the chain is the audit trail.

The database enforces this rather than trusting the service layer to remember:
see the trigger in the migration.

What this table deliberately does NOT do
----------------------------------------
It does not block anything. The platform never writes to SAP, so it cannot stop
a dispatch, and pretending otherwise in the data model would be a lie told in
schema form. Recording the attestation is half the value; **detecting its
absence** is the other half, and that is
:mod:`app.initiatives.i8.exceptions`.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def new_attestation_id() -> str:
    """A stable public identifier, generated here rather than by the database.

    A UUID rather than a sequence for one specific reason: the id is quoted in
    an audit trail, and a sequence leaks how many attestations exist and lets
    two environments mint colliding ids for different records.
    """
    return f"ATT-{uuid.uuid4().hex[:12].upper()}"


class RepairAttestation(Base):
    """One condition-to-repair judgement, as made by one person at one time.

    Fields follow section 11 of the build plan. Names are snake_case here and
    camelCase on the wire, the same way every other I08 model works.
    """

    __tablename__ = "i8_attestation"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=new_attestation_id
    )

    # --- what was assessed ----------------------------------------------

    material_id: Mapped[str] = mapped_column(String(40), index=True)
    """**Normalised before storing.** Ruling 5.1: never compare two raw material
    numbers. An attestation typed against '000000008000005632' and a repair line
    read as '8000005632' are about the same part, and a register that cannot see
    that raises a missing-attestation exception against a part that has one."""

    plant: Mapped[str] = mapped_column(String(8), index=True)

    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3))

    serial_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Optional, and optional on purpose. I08 works at material-plant grain
    today; serial grain is where this is going. Capturing the serial when
    somebody happens to know it costs nothing now and is unrecoverable later."""

    # --- the judgement ---------------------------------------------------

    condition_description: Mapped[str] = mapped_column(Text)
    """Free text. The part a human actually reads."""

    fault_category: Mapped[str] = mapped_column(String(64), index=True)
    """From a controlled list in configuration -- never hard-coded, the same
    rule every other I08 convention follows. VZI will change this list."""

    recommendation: Mapped[str] = mapped_column(String(32), index=True)
    """REPAIRABLE / BEYOND_ECONOMICAL_REPAIR / SCRAP."""

    evidence_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """**A reference string only.** File upload is descoped and SharePoint is
    not provisioned, so this holds a pointer somebody can follow -- a photo
    reference, a report number -- and the platform does not pretend to store
    the artefact."""

    # --- who and when ----------------------------------------------------

    attestor: Mapped[str] = mapped_column(String(128), index=True)
    """User id. Set by the server from the caller, never taken from the body --
    an audit record whose author is self-declared is not an audit record."""

    attested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    """**Server-set, UTC.** Not client-supplied: the whole value of the record
    is that the time is not the attestor's to choose."""

    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Always null today. FR-8 session linkage is not in I08's scope, and the
    column exists so that when it is, it is not a migration on a table that by
    then holds audit history."""

    # --- what it was declared against -------------------------------------

    repair_document: Mapped[str | None] = mapped_column(String(20), nullable=True)
    """EKPO.EBELN of the repair line this was declared against, or null.

    FR-4 asks for the attestation to be linked "once raised, to the repair PR
    or PO". This is that link, and it is the EXACT key the register is built
    on, so a declaration recorded from a repair line needs no matching rule at
    all -- see :func:`app.initiatives.i8.attestation.coverage`.

    **Nullable, and it has to stay that way.** The control exists for the part
    coming off a machine, which is assessed BEFORE any repair is raised; in
    that moment there is no purchase order to point at. Those declarations are
    stored unbound and fall back to the material + plant + date window.
    """

    repair_item: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """EKPO.EBELP of that line. Set with ``repair_document`` or not at all --
    half a key matches nothing and is rejected at validation."""

    # --- the amendment chain ---------------------------------------------

    supersedes: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("i8_attestation.id"), nullable=True, index=True
    )
    """The attestation this one replaces, or null for an original.

    Self-referential on purpose: an amendment is the same kind of thing as the
    record it amends, made later by somebody who knew more. Following the chain
    backwards gives the full history; the row with nothing pointing at it is
    the current view."""

    __table_args__ = (
        # The exception check's access pattern, in one index: "is there an
        # attestation for this material at this plant, and when?" runs once per
        # repair line per refresh over 1,225 lines.
        Index("ix_i8_attestation_material_plant_at", "material_id", "plant", "attested_at"),
        # The exact-bind lookup: "what was declared against THIS repair line?"
        # Runs once per refresh alongside the index above, which stays because
        # the window fallback still needs it for unbound declarations.
        Index("ix_i8_attestation_repair_line", "repair_document", "repair_item"),
    )

    def __repr__(self) -> str:
        return (
            f"<RepairAttestation {self.id} {self.material_id}@{self.plant} "
            f"{self.recommendation} by {self.attestor}>"
        )


# --- W5.5: the coding screen's verdicts -----------------------------------


class CodingVerdict(Base):
    """One model verdict about one material's text, kept so it is not re-bought.

    Why this table exists
    ---------------------
    The screen costs roughly six seconds per material, strictly sequential, and
    it ran entirely in process memory. Three things followed from that, and all
    three were defects rather than trade-offs:

    * a restart lost every verdict, so the screen reported UNSCREENED again;
    * a SAP reload did not clear it, so verdicts outlived the text they judged;
    * with more than one worker, two requests could see different answers.

    What it is NOT
    --------------
    **Not an audit record.** :class:`RepairAttestation` above is immutable
    because it records what a person judged; this records what a model said,
    and a later run with a better model SHOULD replace it. So a row here is
    updated in place, and nothing chains.

    It is a cache with a correctness rule, and the rule is
    :attr:`text_fingerprint`.

    The fingerprint, and why it is the key
    --------------------------------------
    A verdict is only about the words it was shown. Keying on the material
    alone would serve a stale judgement after somebody re-worded the PO line --
    the exact case the screen exists to catch. Keying on the material AND a
    digest of its distinct texts means a reload re-screens the materials whose
    text moved and reuses the rest, which is what makes a four-minute run
    affordable after the first one.

    UNSCREENED is never stored. A stub run, a missing credential and an
    unreadable reply all produce it, and persisting any of them would turn "no
    model has looked at this" into a cached answer -- the one failure this
    module is built to avoid.
    """

    __tablename__ = "i8_coding_verdict"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid.uuid4().hex
    )

    material_id: Mapped[str] = mapped_column(String(40), index=True)
    """Normalised, the same as everywhere else in I08 (ruling 5.1)."""

    text_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    """SHA-256 of this material's distinct texts, normalised and ordered.
    See :func:`app.initiatives.i8.coding_candidates.text_fingerprint`."""

    # --- what the model said ---------------------------------------------

    verdict: Mapped[str] = mapped_column(String(32), index=True)
    """Never ``UNSCREENED`` -- see the class docstring."""

    confidence: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str] = mapped_column(Text, default="")

    # --- who answered, so a verdict can be re-judged later ----------------

    model: Mapped[str] = mapped_column(String(128), default="")
    provider: Mapped[str] = mapped_column(String(32), default="")
    """``foundry`` / ``openai``. Stored so that when the deployment changes,
    the verdicts produced by the old one can be found and re-run."""

    prompt_version: Mapped[int | None] = mapped_column(nullable=True)
    """Which registry prompt version produced it. Same reason as ``provider``:
    a prompt change invalidates judgements the old prompt made."""

    screened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        # One verdict per (material, exact text). The upsert depends on it, and
        # without it a race between two workers leaves two rows and the screen
        # picks whichever the database returns first.
        Index(
            "uq_i8_coding_verdict_material_text",
            "material_id",
            "text_fingerprint",
            unique=True,
        ),
    )


# --- FR-11: working an exception, not just seeing it ----------------------


#: Guards :func:`new_exception_event_id`'s monotonic counter. Ids are minted
#: under whatever thread the request lands on, and two threads reading the
#: clock at once must not be handed the same stamp.
_event_id_lock = threading.Lock()

#: The last microsecond stamp handed out by this process. See the function.
_last_event_stamp = 0


def new_exception_event_id() -> str:
    """A stable, **strictly increasing** id for one entry in a history.

    Deliberately unlike :func:`new_attestation_id`, which is a plain UUID.
    That one is a lone record and its id only has to be unique; these are read
    as a sequence, and ``ORDER BY at, id`` has to put them in the order they
    happened. ``at`` alone cannot do it: two entries recorded in the same tick
    share a timestamp, and the tiebreak then decides the order a person reads
    the history in.

    So the id carries the time -- microseconds since the epoch, hex, fixed
    width so that sorting it as text sorts it as a number.

    **The counter is not decoration.** ``time.time_ns()`` is backed by the
    system clock, and on Windows that clock advances in ~15ms steps: every
    call inside one step returns the *same* value. Two events a millisecond
    apart therefore get identical stamps, and without the bump below the
    tiebreak falls through to the random suffix -- which is how the history
    came back with an assignment displayed after the comment that followed it.
    That was caught by a test that failed roughly one run in three.

    Bumping by one microsecond when the clock has not moved keeps ids strictly
    increasing within a process. The value is an ordering key, not a reading of
    the clock; the time anybody reads is the ``at`` column, which is set by the
    database.

    Across processes two events in the same microsecond remain possible, and
    the random suffix keeps them distinct. Their relative order is then decided
    by ``at``, which has microsecond precision on Postgres and Azure SQL.
    """
    global _last_event_stamp
    with _event_id_lock:
        stamp = time.time_ns() // 1_000
        if stamp <= _last_event_stamp:
            stamp = _last_event_stamp + 1
        _last_event_stamp = stamp
    return f"EXE-{stamp:014X}{uuid.uuid4().hex[:6].upper()}"


class ExceptionEvent(Base):
    """One thing that happened to an exception-queue item, and the ONLY thing
    this module stores about one.

    The items themselves are not stored. They are recomputed from the register
    and the attestation table on every read, which is what keeps the queue from
    drifting away from the data: an item exists exactly as long as its check
    still finds it.

    What a check cannot derive is who owns a finding and what state the work is
    in -- and that is not stored either, as a value. It is **folded from this
    log**: the last status an entry set, the owner the last ``ASSIGNED`` entry
    named. An item nobody has touched has no rows here at all and folds to OPEN
    and unassigned, which is the truth about it rather than a default.

    Append-only, like :class:`RepairAttestation` and
    ``assistant_session``, and for the same reason: there is no UPDATE to make,
    so there is no edit anybody can make to a history afterwards. It is also
    why the API exposes one POST and no PATCH -- see ``tests/test_write_paths``.

    Keyed by the exception's own deterministic id
    (``EX-MISSING_ATTESTATION-4500000677-20``), so the history re-attaches
    whenever the check raises the same finding again. Not a foreign key: there
    is no table of exceptions to point at, by design.
    """

    __tablename__ = "i8_exception_event"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=new_exception_event_id
    )
    exception_id: Mapped[str] = mapped_column(String(128), index=True)

    kind: Mapped[str] = mapped_column(String(16), index=True)
    """ASSIGNED / COMMENT / STATUS / AUTO_RESOLVED.

    AUTO_RESOLVED is kept distinct from STATUS on purpose. "Priya resolved
    this" and "the check stopped finding it" are different claims, and a
    history that renders them the same way invites the first to be read where
    only the second happened."""

    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """The status this entry set, for STATUS and AUTO_RESOLVED: OPEN or
    CLOSED. Null on a comment or an assignment, which do not move the item.
    Rows written before 21-Oct-2026 may say IN_PROGRESS or RESOLVED; they are
    read through ``LEGACY_STATUSES``, never rewritten."""

    owner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    """Who the item was assigned to, on an ``ASSIGNED`` entry.

    Null on every other kind, and null on an ``ASSIGNED`` entry means the item
    was UNASSIGNED -- handing something back is an action, and the kind is what
    tells the two nulls apart."""

    actor: Mapped[str] = mapped_column(String(128))
    """Set by the server from the caller, never from the body -- the same rule
    as the attestor on an attestation. ``system`` for AUTO_RESOLVED."""

    text: Mapped[str] = mapped_column(Text, default="")
    """What was said, or why the item resolved itself."""

    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        # The drawer's access pattern: one exception's history, oldest first.
        Index("ix_i8_exception_event_exception_at", "exception_id", "at"),
    )

    def __repr__(self) -> str:
        return f"<ExceptionEvent {self.id} {self.exception_id} {self.kind} by {self.actor}>"
