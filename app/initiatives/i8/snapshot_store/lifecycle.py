"""The API's side of the stored I08 snapshot: load it, or build it in a process.

``service._build_locked`` calls :func:`load_or_build` in place of building in
the web worker when ``I8_SNAPSHOT_STORE=sql``. Everything around it -- the
background thread, the 503 "building" while it runs, the fingerprint watcher
-- is unchanged: the thread now waits on a build process instead of being one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_APP_ROOT = Path(__file__).resolve().parents[4]


def enabled() -> bool:
    return get_settings().i8_snapshot_store == "sql"


def run_build(reason: str, *, force: bool = False) -> int:
    """Run one build process and wait for it. Returns its exit code."""
    command = [sys.executable, "-m", "app.initiatives.i8.snapshot_store", "build", "--reason", reason]
    if force:
        command.append("--force")
    logger.info("I08 snapshot: starting a build process (%s)", reason)
    process = subprocess.Popen(command, cwd=_APP_ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL)
    code = process.wait()
    logger.info("I08 snapshot: build process finished with code %s (%s)", code, reason)
    return code


def load_or_build(db: Session, reason: str, *, rebuild: bool):
    """The newest stored snapshot -- after a build process, when ``rebuild`` or
    when none is stored yet.

    ``rebuild`` is the watcher seeing the source move (or a manual refresh).
    Without it a stored version is served even if it is behind: the watcher
    then notices the difference and asks for a rebuild, while the stale
    version answers in the meantime instead of a 503.

    Builds are capped at ``I8_BUILD_MAX_ATTEMPTS`` failures an hour, so a build
    that fails the same way every time does not run every minute forever. (A
    ``reason`` starting "manual" is exempt and forces the build.) A build run by
    hand -- ``python -m app.initiatives.i8.snapshot_store build --force`` -- is
    picked up by :func:`newer_stored`, through the fingerprint watcher.
    """
    from app.initiatives.i8.snapshot_store import store

    manual = reason.startswith("manual")
    if not rebuild:
        stored = store.latest_ready(db)
        if stored is not None:
            logger.info("I08 snapshot: serving stored v%s (built %s)", stored.version, stored.built_at)
            return store.load(stored)

    if not manual:
        failures = store.recent_failures(db)
        limit = get_settings().i8_build_max_attempts
        if failures >= limit:
            raise RuntimeError(
                f"{failures} I08 snapshot builds failed in the last hour; not retrying automatically. "
                "`python -m app.initiatives.i8.snapshot_store build --force` starts one."
            )

    db.rollback()  # end the read transaction: the build takes minutes
    code = run_build(reason, force=manual)
    db.rollback()
    stored = store.latest_ready(db)
    if code != 0 or stored is None:
        last = store.latest(db)
        error = last.error if last is not None and last.status == store.FAILED else None
        raise RuntimeError(error or f"the I08 build process exited with code {code}")
    return store.load(stored)


def newer_stored(db: Session, built_at):
    """The stored snapshot when one newer than ``built_at`` is ready, else None.

    How a version built by another process -- by hand, or by the other
    container during a restart -- reaches this one without a rebuild here.
    """
    from app.initiatives.i8.snapshot_store import store

    stored = store.latest_ready(db)
    if stored is None or stored.built_at is None or built_at is None or stored.built_at <= built_at:
        return None
    logger.info("I08 snapshot: loading stored v%s (built %s)", stored.version, stored.built_at)
    return store.load(stored)
