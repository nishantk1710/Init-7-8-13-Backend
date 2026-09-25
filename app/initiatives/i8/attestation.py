"""W5.3 -- recording a condition-to-repair attestation, and reading it back.

What an attestation is, in plain English
----------------------------------------
When a repairable part comes off a machine, somebody is supposed to look at it
and record a judgement: is this repairable, is it beyond economical repair, or
is it scrap. That record is the condition-to-repair attestation.

Today nobody records it anywhere. So parts get sent for repair with no
assessment, and the platform has no way to tell a considered decision from a
reflex.

**We cannot enforce this.** The platform never writes to SAP, so it cannot block
a dispatch. What it can do is two things: record the attestation when somebody
makes one -- this module -- and detect its absence, which is
:mod:`app.initiatives.i8.exceptions`. The second half is where the value is,
because it makes the gap visible.

The write path, and why it is the only one
-------------------------------------------
This is **the first thing I08 writes**. W5.1 and W5.2 are read models, and there
is a contract test asserting the module exposes nothing but ``GET``. That test
has been changed rather than deleted -- from "nothing writes" to "only
attestations write, and only to our own table, never to SAP". The distinction is
the whole guarantee, so it is asserted rather than described.

Immutability
------------
Attestations are never updated. An amendment is a new row whose ``supersedes``
points at the one it replaces, and the original stays readable forever. Rewriting
an audit record does not correct history, it destroys it.

The service layer never issues an UPDATE, and on Postgres a trigger makes it an
error if anything ever tries -- see the migration. Belt and braces, because "the
service layer does not do that" is a convention and conventions decay.

Never invent, never default
---------------------------
Rule 4 gets *harder* here, not easier. "This part has no attestation" and "we
have not looked for one yet" are different states and both want to render as
"no attestation". :class:`AttestationCoverage` keeps them apart even where the
UI collapses them.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.initiatives.i8.models import RepairAttestation
from app.shared.plant_scope import IN_SCOPE_PLANTS, is_in_scope

logger = get_logger(__name__)


class Recommendation(str, Enum):
    """What the assessor concluded.

    A fixed vocabulary rather than configuration, unlike the fault categories:
    these three map one-for-one onto the frontend's ``DeclarationCondition``
    (``Repairable`` / ``Beyond Economical Repair`` / ``Scrap``), so adding a
    fourth is a UI change and a conversation, not an ``.env`` edit.
    """

    REPAIRABLE = "REPAIRABLE"
    BEYOND_ECONOMICAL_REPAIR = "BEYOND_ECONOMICAL_REPAIR"
    SCRAP = "SCRAP"


#: Recommendation -> the exact string the frontend's DeclarationCondition uses.
#: Checked against src/features/initiative-8/types/repair.ts, not from memory.
CONDITION_LABELS: dict[Recommendation, str] = {
    Recommendation.REPAIRABLE: "Repairable",
    Recommendation.BEYOND_ECONOMICAL_REPAIR: "Beyond Economical Repair",
    Recommendation.SCRAP: "Scrap",
}


class AttestationError(ValueError):
    """The submitted attestation cannot be recorded, and why."""


@dataclass(frozen=True)
class AttestationDraft:
    """A submitted attestation, before it is validated and stored.

    Deliberately not the ORM model: what a caller may supply and what gets
    written are different sets of fields. ``attestor`` and ``attested_at`` are
    server-set -- an audit record whose author and timestamp are the author's
    to choose is not an audit record.
    """

    material_id: str
    plant: str
    quantity: Decimal
    condition_description: str
    fault_category: str
    recommendation: str
    serial_number: str | None = None
    evidence_reference: str | None = None
    supersedes: str | None = None


def validate(
    draft: AttestationDraft,
    cfg: I8Settings | None = None,
    *,
    known_materials: Collection[str] | None = None,
) -> AttestationDraft:
    """Check a draft and return it normalised, or raise :class:`AttestationError`.

    Separate from :func:`record` so the same rules can be unit-tested without a
    database, and so the API layer can turn one exception type into one status
    code rather than guessing at a dozen.

    The table is append-only, so everything that can be checked is checked
    here: a row that gets past this can never be taken back. ``known_materials``
    is the repairable universe when the caller holds it -- the API does -- and
    None skips that one check for callers that do not.
    """
    cfg = cfg or get_i8_settings()

    material_id = normalise(draft.material_id)
    if not material_id:
        raise AttestationError("materialId is required")

    if not is_eighty_series(material_id, cfg):
        raise AttestationError(
            f"materialId {material_id!r} is not an 80-series repairable part, so "
            "there is no repair for a condition-to-repair attestation to cover"
        )

    if known_materials is not None and material_id not in known_materials:
        raise AttestationError(
            f"materialId {material_id!r} is not in the repairable universe -- no "
            "material master, stock or purchasing record knows it"
        )

    plant = (draft.plant or "").strip()
    if not plant:
        raise AttestationError("plant is required")

    if not is_in_scope(plant):
        raise AttestationError(
            f"plant {plant!r} is outside the platform's scope: "
            f"{', '.join(IN_SCOPE_PLANTS)}"
        )

    if draft.quantity is None or Decimal(draft.quantity) <= 0:
        raise AttestationError("quantity must be greater than zero")

    if Decimal(draft.quantity) > cfg.attestation_max_quantity:
        raise AttestationError(
            f"quantity {draft.quantity} is more than {cfg.attestation_max_quantity} "
            "-- almost certainly a typo, and the record could never be corrected "
            "in place"
        )

    description = (draft.condition_description or "").strip()
    if not description:
        raise AttestationError(
            "conditionDescription is required -- it is the part a human reads"
        )

    fault_category = (draft.fault_category or "").strip().upper()
    if fault_category not in cfg.fault_category_list:
        raise AttestationError(
            f"faultCategory {draft.fault_category!r} is not in the configured "
            f"list: {', '.join(cfg.fault_category_list)}"
        )

    recommendation = (draft.recommendation or "").strip().upper()
    if recommendation not in {r.value for r in Recommendation}:
        raise AttestationError(
            f"recommendation {draft.recommendation!r} must be one of "
            f"{', '.join(r.value for r in Recommendation)}"
        )

    return AttestationDraft(
        # Normalised before storing. Ruling 5.1: an attestation typed against
        # '000000008000005632' and a repair line read as '8000005632' are about
        # the same part, and a register that cannot see that raises a
        # missing-attestation exception against a part that has one.
        material_id=material_id,
        plant=plant,
        quantity=Decimal(draft.quantity),
        condition_description=description,
        fault_category=fault_category,
        recommendation=recommendation,
        serial_number=(draft.serial_number or "").strip() or None,
        evidence_reference=(draft.evidence_reference or "").strip() or None,
        supersedes=(draft.supersedes or "").strip() or None,
    )


def record(
    db: Session,
    draft: AttestationDraft,
    *,
    attestor: str,
    cfg: I8Settings | None = None,
    known_materials: Collection[str] | None = None,
) -> RepairAttestation:
    """Store one attestation and return it. Never updates anything.

    ``attestor`` is passed separately from the draft on purpose: it comes from
    the authenticated caller, not from the request body.
    """
    cfg = cfg or get_i8_settings()
    clean = validate(draft, cfg, known_materials=known_materials)

    if clean.supersedes is not None:
        original = db.get(RepairAttestation, clean.supersedes)
        if original is None:
            raise AttestationError(
                f"supersedes {clean.supersedes!r} does not exist -- an amendment "
                "must point at the attestation it replaces"
            )
        # An amendment that silently moved the part it is about would break the
        # chain's meaning: the history would read as one part's assessment when
        # it is two.
        if original.material_id != clean.material_id or original.plant != clean.plant:
            raise AttestationError(
                f"an amendment must be for the same material and plant as "
                f"{original.id} ({original.material_id} at {original.plant})"
            )

    attestation = RepairAttestation(
        material_id=clean.material_id,
        plant=clean.plant,
        quantity=clean.quantity,
        condition_description=clean.condition_description,
        fault_category=clean.fault_category,
        recommendation=clean.recommendation,
        serial_number=clean.serial_number,
        evidence_reference=clean.evidence_reference,
        attestor=attestor,
        # Server-set, UTC. Not client-supplied -- the value of the record is
        # that the time is not the attestor's to choose.
        attested_at=datetime.now(timezone.utc),
        # Always null today. Linking an attestation to its assistant session is
        # FR-4 / FR-8 and ours; nothing supplies the id yet -- the reservation
        # that would carry it (RESB.BEDNR) is not exposed.
        session_id=None,
        supersedes=clean.supersedes,
    )
    db.add(attestation)
    db.commit()
    db.refresh(attestation)

    logger.info(
        "I08 attestation %s recorded: %s at %s, %s, by %s%s",
        attestation.id,
        attestation.material_id,
        attestation.plant,
        attestation.recommendation,
        attestor,
        f", superseding {attestation.supersedes}" if attestation.supersedes else "",
    )
    return attestation


def find(
    db: Session,
    *,
    material_id: str | None = None,
    plant: str | None = None,
    include_superseded: bool = True,
) -> list[RepairAttestation]:
    """Attestations, newest first, optionally narrowed to a material or plant.

    ``include_superseded`` defaults to True because this is an audit record and
    the superseded rows are the history -- hiding them by default would make the
    amendment chain invisible, which is the one thing it exists to be.
    """
    statement = select(RepairAttestation)

    if material_id is not None:
        # Normalised both sides. Never compare two raw material numbers.
        key = normalise(material_id)
        if key is None:
            return []
        statement = statement.where(RepairAttestation.material_id == key)

    if plant is not None:
        statement = statement.where(RepairAttestation.plant == plant.strip())

    rows = list(db.execute(statement.order_by(RepairAttestation.attested_at.desc())).scalars())

    if not include_superseded:
        amended = {r.supersedes for r in rows if r.supersedes}
        rows = [r for r in rows if r.id not in amended]

    return rows


# --- Matching an attestation to a repair line -----------------------------


@dataclass(frozen=True)
class AttestationCoverage:
    """Which repair lines have an attestation, and which do not.

    Two distinct absences, kept apart deliberately (rule 4). A line in
    ``uncovered`` was checked and has nothing. A line in neither mapping was
    never checked -- because it was outside the scope of this run -- and that is
    not the same statement. The UI may collapse them; the data must not.
    """

    #: (document, item) -> the attestation that covers it, newest first match.
    covered: dict[tuple[str, str], RepairAttestation]

    #: (document, item) for lines that were checked and have no attestation.
    uncovered: set[tuple[str, str]]

    #: The window that produced this answer, so a result can be explained.
    window_days: int

    @property
    def checked(self) -> int:
        return len(self.covered) + len(self.uncovered)


def _within_window(
    attested_at: datetime, raised_at: date | None, window: timedelta
) -> bool:
    """Whether an attestation is close enough in time to a repair line.

    A line with no raised date is covered by any attestation for its
    material-plant. ERDAT is populated on all 1,225 repair lines today, so this
    is a guard rather than a live case -- but "no date" must not silently mean
    "never matches", which would raise an exception nobody can clear.
    """
    if raised_at is None:
        return True
    attested_on = attested_at.date()
    return raised_at - window <= attested_on <= raised_at + window


def explain_coverage(
    attestation: RepairAttestation,
    lines,
    cfg: I8Settings | None = None,
) -> tuple[list[str], str]:
    """Which repair lines one attestation covers, and a sentence saying why.

    Answers the question a person asks immediately after submitting a form:
    *did that do anything?* It lives here, beside :func:`coverage`, because it
    must apply the SAME window rule -- an explanation derived from a second
    implementation of the rule would eventually contradict the queue it is
    explaining.

    Returns ``(line ids, note)``. The note is written to be read by a user.
    """
    cfg = cfg or get_i8_settings()
    window = timedelta(days=cfg.attestation_window_days)

    same_part = [
        line
        for line in lines
        if line.material_id == attestation.material_id
        and line.plant == attestation.plant
    ]
    covered = [
        line
        for line in same_part
        if _within_window(attestation.attested_at, line.raised_at, window)
    ]

    if covered:
        return (
            [f"{line.purchasing_document}-{line.item}" for line in covered],
            (
                f"Recorded, and it covers {len(covered)} repair "
                f"line{'s' if len(covered) != 1 else ''} for "
                f"{attestation.material_id} at plant {attestation.plant}."
            ),
        )

    if not same_part:
        return (
            [],
            (
                f"Recorded. No repair line exists for {attestation.material_id} "
                f"at plant {attestation.plant} in this extract, so it covers "
                "nothing yet -- which is the normal case for a part assessed "
                "before it is sent anywhere."
            ),
        )

    # The trap. The part HAS repair lines; they are all outside the window.
    dates = [line.raised_at for line in same_part if line.raised_at]
    nearest = max(dates) if dates else None
    gap = (
        abs((attestation.attested_at.date() - nearest).days)
        if nearest is not None
        else None
    )
    return (
        [],
        (
            f"Recorded, but it covers none of the {len(same_part)} repair "
            f"line{'s' if len(same_part) != 1 else ''} for this part. The most "
            f"recent was raised {nearest.isoformat() if nearest else 'unknown'}"
            + (f", {gap} days from this assessment" if gap is not None else "")
            + f" -- outside the {cfg.attestation_window_days}-day matching "
            "window. This extract is a frozen July-2026 snapshot, so a "
            "new assessment cannot retrospectively cover a repair that was "
            "dispatched long ago. The queue will still show those lines as "
            "outstanding, and that is the correct answer rather than a fault."
        ),
    )


def coverage(
    db: Session,
    lines,
    cfg: I8Settings | None = None,
) -> AttestationCoverage:
    """Match every repair line to an attestation, in one pass.

    **A batch check, run once per refresh cycle, not per request.** The register
    is 1,225 lines and the snapshot is already cached for exactly this reason;
    a per-request lookup would put a query per line on the page load W5.4 is
    rendering.

    The matching key is material + plant + a date window. That is the only key
    both sides share -- an attestation is made against a physical part coming
    off a machine, and nothing in that moment carries the purchase-order number
    the part will later be repaired under. **It is a proposal, not a confirmed
    rule** (open question 3), which is why the window comes from configuration
    and travels with every answer.
    """
    cfg = cfg or get_i8_settings()
    window = timedelta(days=cfg.attestation_window_days)

    # One query for every attestation, then matched in Python. The table holds
    # the handful of rows this control has ever produced -- there is nothing to
    # gain from narrowing it, and a per-line query is what this is avoiding.
    by_key: dict[tuple[str, str], list[RepairAttestation]] = {}
    for attestation in find(db, include_superseded=False):
        by_key.setdefault((attestation.material_id, attestation.plant), []).append(
            attestation
        )

    covered: dict[tuple[str, str], RepairAttestation] = {}
    uncovered: set[tuple[str, str]] = set()

    for line in lines:
        if line.plant is None:
            # No plant means the material-plant key cannot be formed at all.
            # That is a data gap, not a missing attestation, and calling it one
            # would put a line in the queue that nobody can ever clear.
            continue

        candidates = by_key.get((line.material_id, line.plant), [])
        match = next(
            (
                a
                for a in candidates
                if _within_window(a.attested_at, line.raised_at, window)
            ),
            None,
        )
        if match is not None:
            covered[line.key] = match
        else:
            uncovered.add(line.key)

    logger.info(
        "I08 attestation coverage: %d of %d repair lines covered (window +/-%dd)",
        len(covered),
        len(covered) + len(uncovered),
        cfg.attestation_window_days,
    )
    return AttestationCoverage(
        covered=covered,
        uncovered=uncovered,
        window_days=cfg.attestation_window_days,
    )
