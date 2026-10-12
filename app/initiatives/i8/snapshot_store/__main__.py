"""Build the I08 snapshot in its own process and store it.

    python -m app.initiatives.i8.snapshot_store build [--reason TEXT] [--force]

This is how the API builds when ``I8_SNAPSHOT_STORE=sql``
(``lifecycle.run_build``): a separate process, not a thread in the web worker.
In the worker the build competed with every request for memory and the GIL --
on the production extract it took the worker past 600 MB, missed gunicorn's
heartbeat, and the worker was replaced with the build lost. As its own process
it can be as slow as it needs; if it dies it dies alone, and the API keeps
serving the last stored version.

Holds the I08 build lock (a SQL Server application lock) for its whole run, so
two API processes -- Azure runs the new container beside the old during a
restart -- never build at once: the second waits, then finds the first one's
result current and exits. It also waits while an I13 build holds that lock:
built side by side the two starved the one-vCore Azure SQL.

Without ``--force`` a build whose stored version already matches the source
data does nothing. Exit codes: 0 built (or already current), 1 failed.
"""

from __future__ import annotations

import argparse
import sys
import time

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

EXIT_BUILT = 0
EXIT_FAILED = 1

LOCK_NAME = "spares_ai_i8_snapshot_build"
_POLL_SECONDS = 15
#: How long to wait for another I08 build, or for an I13 one, before giving up.
_MAX_WAIT_SECONDS = 3 * 60 * 60


def _wait(what: str, busy) -> bool:
    """Poll ``busy()`` until it is False. False if it never cleared in time."""
    waited = 0
    while busy():
        if waited == 0:
            logger.info("I08 snapshot build waiting for %s", what)
        if waited >= _MAX_WAIT_SECONDS:
            return False
        time.sleep(_POLL_SECONDS)
        waited += _POLL_SECONDS
    return True


def build(reason: str, *, force: bool = False) -> int:
    from datetime import date

    from app.ingest.scheduler import _Lock
    from app.initiatives.i8.config import get_i8_settings
    from app.initiatives.i8.service import _describe, build_snapshot, source_state
    from app.initiatives.i8.snapshot_store import store

    lock = _Lock(LOCK_NAME)
    if not _wait("the I08 build already running", lambda: not lock.acquire()):
        logger.error("I08 snapshot build: another build held the lock for too long; giving up")
        return EXIT_FAILED

    db = get_sessionmaker()()
    version = None
    try:
        abandoned = store.abandon_unfinished(db)
        if abandoned:
            logger.info("I08 snapshot: cleared %d build(s) that stopped before finishing", abandoned)

        cfg = get_i8_settings()
        current = source_state(db, reference_date=cfg.reference_date_value or date.today())
        stored = store.latest_ready(db)
        if not force and stored is not None and stored.fingerprint == current.fingerprint:
            logger.info("I08 snapshot: v%s is already current; nothing to build (%s)", stored.version, reason)
            return EXIT_BUILT
        db.rollback()

        from app.initiatives.i13.snapshot_store.build_lock import held as i13_building

        if not _wait("the I13 build to finish", i13_building):
            logger.warning("I08 snapshot build: an I13 build is still running after the wait; building anyway")

        version = store.begin(db, reason=reason)
        logger.info("I08 snapshot: build v%s started (%s)", version, reason)
        snapshot = build_snapshot(db, cfg)
        size = store.finish(db, version, snapshot)
        logger.info(
            "I08 snapshot: v%s ready in %.1fs -- %d repair lines, %d universe rows, %.1f MB stored",
            version, snapshot.build_seconds, len(snapshot.lines), len(snapshot.universe), size / 2**20,
        )
        return EXIT_BUILT
    except Exception as exc:  # noqa: BLE001 -- recorded on the run row and logged
        logger.exception("I08 snapshot build failed (%s)", reason)
        if version is not None:
            store.fail(db, version, _describe(exc))
        return EXIT_FAILED
    finally:
        db.close()
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.initiatives.i8.snapshot_store")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("build", help="build the I08 snapshot and store it in Azure SQL")
    run.add_argument("--reason", default="command line")
    run.add_argument("--force", action="store_true", help="build even when the stored version is current")
    arguments = parser.parse_args(argv)
    configure_logging(get_settings())
    if arguments.command == "build":
        return build(arguments.reason, force=arguments.force)
    return 2  # pragma: no cover - argparse rejects anything else


if __name__ == "__main__":
    sys.exit(main())
