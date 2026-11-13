"""The signed-in users' names and e-mails, keyed by Entra object ID.

:func:`remember` is called on every validated sign-in (``app.core.security``)
and records a user the first time they are seen, or again when their name or
e-mail changed. It never slows or fails a request: an in-process memory skips
users already recorded, and the database write runs on a background thread
whose errors are logged and dropped.

:func:`names_for` / :func:`display_name` are for screens and reports that show
who did something: an author column holds the object ID, and these turn it into
"Name <e-mail>" -- or return the ID unchanged when it is not a known user (an
``X-Actor-Id`` value recorded before sign-in, or a placeholder).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.app_user import AppUser

if TYPE_CHECKING:
    from app.core.security import CurrentUser

logger = get_logger(__name__)

#: How long a user, once recorded, is not checked again by this process.
RECHECK_SECONDS = 6 * 3600

#: A word of a name: letters and digits, so "O'Brien" is two words and "Patil," is one.
_WORD = re.compile(r"\w+")

_seen: dict[str, tuple[str, str, float]] = {}
_seen_lock = threading.Lock()
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="user-directory")


def record(db: Session, oid: str, name: str, email: str) -> bool:
    """Append a row unless the latest one for ``oid`` already says the same. True when added."""
    latest = db.execute(
        select(AppUser.name, AppUser.email).where(AppUser.oid == oid).order_by(AppUser.id.desc()).limit(1)
    ).first()
    if latest is not None and (latest.name, latest.email) == (name, email):
        return False
    db.add(AppUser(oid=oid, name=name, email=email))
    db.commit()
    return True


def _record_in_background(oid: str, name: str, email: str) -> None:
    try:
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as db:
            record(db, oid, name, email)
    except Exception:  # noqa: BLE001 -- a directory write must never matter to the request
        logger.warning("user directory: could not record %s", oid, exc_info=True)
        with _seen_lock:
            _seen.pop(oid, None)  # try again on the next sign-in


def remember(user: CurrentUser) -> None:
    """Record ``user`` if this process has not recently done so. Never raises."""
    if not get_settings().database_url:
        return
    now = time.monotonic()
    with _seen_lock:
        seen = _seen.get(user.oid)
        if seen is not None and seen[:2] == (user.name, user.email) and now - seen[2] < RECHECK_SECONDS:
            return
        _seen[user.oid] = (user.name, user.email, now)
    try:
        _writer.submit(_record_in_background, user.oid, user.name, user.email)
    except RuntimeError:  # the executor is shut down at interpreter exit
        pass


def names_for(db: Session, oids: Iterable[str]) -> dict[str, tuple[str, str]]:
    """``oid -> (name, email)`` for the known users among ``oids`` (latest row each)."""
    wanted = sorted({o for o in oids if o})
    if not wanted:
        return {}
    result: dict[str, tuple[str, str]] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        latest = (
            select(AppUser.oid, func.max(AppUser.id).label("id"))
            .where(AppUser.oid.in_(chunk))
            .group_by(AppUser.oid)
            .subquery()
        )
        rows = db.execute(select(AppUser.oid, AppUser.name, AppUser.email).join(latest, AppUser.id == latest.c.id))
        result.update({r.oid: (r.name, r.email) for r in rows})
    return result


def oid_for_name(db: Session, typed: str | None) -> str | None:
    """The one signed-in user a typed name (or e-mail address) stands for, or ``None``.

    For free text such as the assistant's Requester box, which is never identity on its own: it is only an identity
    when it matches exactly one person this directory knows. Case and surrounding spaces never matter. In order:

    1. The whole name a person signed in with (their latest), or their e-mail address, equals what was typed. If one
       person does, that is the answer; if several share that exact name, nobody is.
    2. Otherwise, at least two words were typed and every one is a whole word in exactly one person's name, in any
       order -- so "Asha Rao" finds "Asha Meera Rao". One word ("Asha"), part of a word ("Ash Rao") or a word
       the name does not have ("Asha Stone") finds nobody.

    Nothing is guessed: no match, or more than one person, is ``None``. Only people who have signed in are known --
    anybody else has no object ID here to notify.
    """
    wanted = " ".join((typed or "").split()).casefold()
    if not wanted:
        return None
    latest = select(AppUser.oid, func.max(AppUser.id).label("id")).group_by(AppUser.oid).subquery()
    known = select(AppUser.oid, AppUser.name).join(latest, AppUser.id == latest.c.id)

    exact = {
        row.oid
        for row in db.execute(
            known.where((func.lower(func.trim(AppUser.name)) == wanted) | (func.lower(func.trim(AppUser.email)) == wanted))
        )
    }
    if exact:
        return next(iter(exact)) if len(exact) == 1 else None

    words = set(_WORD.findall(wanted))
    if "@" in wanted or len(words) < 2:
        return None
    narrowed = known
    for word in words:  # let the database drop the people who cannot match; whole words are checked below
        narrowed = narrowed.where(func.lower(AppUser.name).contains(word, autoescape=True))
    found = {row.oid for row in db.execute(narrowed) if words <= set(_WORD.findall((row.name or "").casefold()))}
    return next(iter(found)) if len(found) == 1 else None


def display_name(db: Session, oid: str) -> str:
    """"Name <e-mail>" for a known user, the bare name or e-mail when only one is
    known, and ``oid`` unchanged otherwise."""
    name, email = names_for(db, [oid]).get(oid, ("", ""))
    if name and email:
        return f"{name} <{email}>"
    return name or email or oid


def forget_all() -> None:
    """Clear the in-process memory (tests)."""
    with _seen_lock:
        _seen.clear()
