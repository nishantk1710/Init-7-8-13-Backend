"""Run one I13 snapshot build, as its own process.

    python -m app.initiatives.i13.snapshot_store build --reason "data changed"

This is how the API starts a build (``lifecycle.start_background_build``): a
separate process, not a thread in the web worker. A build reads and computes
millions of rows; inside the worker, a heavy batch could exhaust its memory or
keep it from answering gunicorn's heartbeat for 120 s, and either way gunicorn
killed the worker -- the website went down and the build was lost with it. As
its own process the build can be as slow as it needs; if it dies, it dies alone,
and the website keeps serving the last ready version.

Holds the build lock (``build_lock``) for its whole run. Exit codes:

    0  built and made the served version
    1  the build failed (the run row carries the error)
    3  another build already holds the lock -- nothing done
"""

from __future__ import annotations

import argparse
import sys

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

EXIT_BUILT = 0
EXIT_FAILED = 1
EXIT_LOCKED = 3


def build(reason: str) -> int:
    from app.initiatives.i13.snapshot_store import builder
    from app.initiatives.i13.snapshot_store.build_lock import BuildLock

    lock = BuildLock()
    if not lock.acquire():
        logger.info("I13 store: another build holds the lock; nothing to do")
        return EXIT_LOCKED
    db = get_sessionmaker()()
    try:
        builder.build(db, reason=reason)
        return EXIT_BUILT
    except Exception:  # noqa: BLE001 -- builder.build logged it and recorded it on the run
        return EXIT_FAILED
    finally:
        db.close()
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.initiatives.i13.snapshot_store")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("build", help="build a new version of the stored I13 snapshot")
    run.add_argument("--reason", default="command line")
    arguments = parser.parse_args(argv)
    configure_logging(get_settings())
    if arguments.command == "build":
        return build(arguments.reason)
    return 2  # pragma: no cover - argparse rejects anything else


if __name__ == "__main__":
    sys.exit(main())
