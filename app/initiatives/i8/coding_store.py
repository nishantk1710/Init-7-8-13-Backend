"""Reading and writing the coding screen's stored verdicts.

Kept out of :mod:`app.initiatives.i8.coding_candidates` on purpose. That module
is about judgement -- which text to read, what the model said, how sure it was
-- and every function in it can be reasoned about without a database. This one
is about storage, and nothing more.

The contract in one line: **a verdict belongs to the exact words it judged.**
:func:`text_fingerprint` is what makes that true, and everything else here is
plumbing around it.

What is deliberately not here
-----------------------------
No TTL, and no "re-screen anything older than N days". A verdict does not decay
with time; it decays when the text changes, when the prompt changes or when the
model changes, and all three of those are recorded on the row so they can be
found and re-run deliberately. A TTL would re-buy four minutes of model calls on
a schedule nobody chose.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.models import CodingVerdict

logger = get_logger(__name__)


def text_fingerprint(texts: Iterable[str]) -> str:
    """A stable digest of the distinct texts a material was judged on.

    Normalised the same way :func:`coding_candidates.normalise_text` normalises
    a twin's text -- upper-cased, whitespace collapsed -- so that re-typing the
    same description with two spaces does not look like a changed line and buy
    a fresh model call.

    Sorted before hashing: the SAME set of texts must produce the same
    fingerprint whichever order the rows came back in, and row order is not
    guaranteed across a reload.

    Newline-joined with the texts themselves stripped of newlines, so two
    different sets cannot collide by concatenating to the same string.
    """
    cleaned = sorted({" ".join(t.split()).upper() for t in texts if t and t.strip()})
    joined = "\n".join(cleaned)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredVerdict:
    """A verdict read back from the table.

    A plain value rather than the ORM object: it outlives the session that
    read it, and a caller holding a detached instance that lazy-loads on
    attribute access is a bug waiting for the first slow day.
    """

    material_id: str
    text_fingerprint: str
    verdict: str
    confidence: str
    reason: str
    model: str
    provider: str
    prompt_version: int | None
    screened_at: datetime


def load_verdicts(
    db: Session, fingerprints: Mapping[str, str]
) -> dict[str, StoredVerdict]:
    """Stored verdicts for these materials, but ONLY where the text still matches.

    ``fingerprints`` maps material -> the fingerprint of the text it is about
    to be judged on. A row whose stored fingerprint differs is not returned:
    the text moved, so the verdict is about something else now.

    One query for the whole screen. The materials are few -- 41 in the measured
    extract -- so they go in a single IN list rather than being chunked.
    """
    if not fingerprints:
        return {}

    rows = db.execute(
        select(CodingVerdict).where(CodingVerdict.material_id.in_(list(fingerprints)))
    ).scalars()

    found: dict[str, StoredVerdict] = {}
    for row in rows:
        if fingerprints.get(row.material_id) != row.text_fingerprint:
            # Same material, different words. Not a hit.
            continue
        found[row.material_id] = StoredVerdict(
            material_id=row.material_id,
            text_fingerprint=row.text_fingerprint,
            verdict=row.verdict,
            confidence=row.confidence or "",
            reason=row.reason or "",
            model=row.model or "",
            provider=row.provider or "",
            prompt_version=row.prompt_version,
            screened_at=row.screened_at,
        )
    return found


def save_verdict(
    db: Session,
    *,
    material_id: str,
    fingerprint: str,
    verdict: str,
    confidence: str,
    reason: str,
    model: str,
    provider: str,
    prompt_version: int | None,
    screened_at: datetime,
) -> None:
    """Record one verdict, replacing any earlier one for the same exact text.

    Updated in place rather than appended: this is a model's opinion, not an
    audit record, and a re-run with a better model should supersede it rather
    than leave two answers for one question. (:class:`RepairAttestation` is the
    opposite, and the difference is stated in both docstrings.)

    SELECT-then-write rather than a dialect ``ON CONFLICT`` / ``MERGE``,
    because this schema stays portable between Postgres and Azure SQL. The
    unique index means two workers can still race, so the insert path catches
    the integrity error and falls back to the update -- the lost race is not an
    error, both workers computed the same thing.

    **The caller must not pass UNSCREENED.** Storing it would cache "nobody
    looked" as though it were an answer.
    """
    existing = db.execute(
        select(CodingVerdict).where(
            CodingVerdict.material_id == material_id,
            CodingVerdict.text_fingerprint == fingerprint,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.verdict = verdict
        existing.confidence = confidence
        existing.reason = reason
        existing.model = model
        existing.provider = provider
        existing.prompt_version = prompt_version
        existing.screened_at = screened_at
        db.commit()
        return

    db.add(
        CodingVerdict(
            id=uuid.uuid4().hex,
            material_id=material_id,
            text_fingerprint=fingerprint,
            verdict=verdict,
            confidence=confidence,
            reason=reason,
            model=model,
            provider=provider,
            prompt_version=prompt_version,
            screened_at=screened_at,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Another worker screened the same material against the same text
        # between the select and the insert. Same answer, so take theirs.
        db.rollback()
        logger.info(
            "I08 coding verdict for %s was written concurrently; keeping the "
            "stored row",
            material_id,
        )


def forget_verdicts(db: Session, materials: Sequence[str] | None = None) -> int:
    """Delete stored verdicts, for a deliberate re-screen. Returns how many went.

    Exists for the day a prompt or a deployment changes and the verdicts it
    produced should be re-run: there is no automatic rule for that, because
    "the model is better now" is a judgement nobody can express as a TTL.
    """
    statement = select(CodingVerdict)
    if materials:
        statement = statement.where(CodingVerdict.material_id.in_(list(materials)))
    rows = list(db.execute(statement).scalars())
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)
