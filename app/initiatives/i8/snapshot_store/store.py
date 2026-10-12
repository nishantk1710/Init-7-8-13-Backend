"""The ``i8_snapshot_run`` rows: start, finish, fail, and the newest ready one."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.snapshot_store import codec
from app.models.i8_snapshot_store import I8SnapshotRun

logger = get_logger(__name__)

BUILDING, READY, FAILED, SUPERSEDED = "building", "ready", "failed", "superseded"

#: Run rows kept for history; older ones are deleted. Only one keeps a payload.
_KEEP_RUNS = 10


def begin(db: Session, *, reason: str) -> int:
    """Insert a ``building`` row and commit it. Returns its version."""
    version = (db.execute(select(func.max(I8SnapshotRun.version))).scalar() or 0) + 1
    db.add(
        I8SnapshotRun(
            version=version,
            status=BUILDING,
            reason=reason[:80],
            codec_version=codec.CODEC_VERSION,
            started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return version


def finish(db: Session, version: int, snapshot) -> int:
    """Store ``snapshot`` under ``version`` and make it the one served."""
    payload = codec.encode(snapshot)
    db.execute(update(I8SnapshotRun).where(I8SnapshotRun.status == READY).values(status=SUPERSEDED, payload=None))
    db.execute(
        update(I8SnapshotRun)
        .where(I8SnapshotRun.version == version)
        .values(
            status=READY,
            fingerprint=snapshot.source_fingerprint,
            reference_date=snapshot.reference_date,
            built_at=snapshot.built_at,
            build_seconds=Decimal(str(round(snapshot.build_seconds, 1))),
            payload=payload,
            payload_bytes=len(payload),
        )
    )
    db.commit()
    _trim(db)
    return len(payload)


def fail(db: Session, version: int, error: str) -> None:
    db.rollback()
    db.execute(
        update(I8SnapshotRun).where(I8SnapshotRun.version == version).values(status=FAILED, error=error[:4000])
    )
    db.commit()


def abandon_unfinished(db: Session) -> int:
    """Mark ``building`` rows failed. Only call while holding the build lock --
    then nothing is building, and a ``building`` row is one whose process died."""
    stale = db.execute(
        update(I8SnapshotRun)
        .where(I8SnapshotRun.status == BUILDING)
        .values(status=FAILED, error="interrupted: the build process stopped before it finished")
    ).rowcount
    db.commit()
    return stale or 0


def _trim(db: Session) -> None:
    old = db.execute(
        select(I8SnapshotRun.version).order_by(I8SnapshotRun.version.desc()).offset(_KEEP_RUNS)
    ).scalars().all()
    if old:
        db.execute(I8SnapshotRun.__table__.delete().where(I8SnapshotRun.version.in_(old)))
        db.commit()


def latest_ready(db: Session) -> I8SnapshotRun | None:
    """The newest ready run this code can decode."""
    return db.execute(
        select(I8SnapshotRun)
        .where(I8SnapshotRun.status == READY, I8SnapshotRun.codec_version == codec.CODEC_VERSION)
        .order_by(I8SnapshotRun.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest(db: Session) -> I8SnapshotRun | None:
    return db.execute(select(I8SnapshotRun).order_by(I8SnapshotRun.version.desc()).limit(1)).scalar_one_or_none()


def load(run: I8SnapshotRun):
    """The snapshot stored in ``run``."""
    return codec.decode(run.payload)


def recent_failures(db: Session, *, within: timedelta = timedelta(hours=1)) -> int:
    """Failed builds started in the last ``within``: the automatic-retry budget."""
    since = datetime.now(timezone.utc) - within
    return db.execute(
        select(func.count())
        .select_from(I8SnapshotRun)
        .where(I8SnapshotRun.status == FAILED, I8SnapshotRun.started_at >= since)
    ).scalar_one()
