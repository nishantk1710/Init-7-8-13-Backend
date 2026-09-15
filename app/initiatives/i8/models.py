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
    )

    def __repr__(self) -> str:
        return (
            f"<RepairAttestation {self.id} {self.material_id}@{self.plant} "
            f"{self.recommendation} by {self.attestor}>"
        )
