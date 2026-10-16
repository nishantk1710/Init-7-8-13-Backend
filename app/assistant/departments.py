"""The department codes the assistant's opener offers, and the check behind it.

Where the codes come from
-------------------------
VZI has no department master in any extract we receive. What it does have is a
habit: the 5-digit department code (``10100`` Finance-Commercial, ``40301`` at
Gamsberg) is keyed into **Requisitioner** (``EBAN.AFNAM``) on every purchase
requisition. So the list is every 5-digit AFNAM on a requisition at an in-scope
plant -- the departments that have actually raised one. Measured on the live
extract (15 Oct 2026): 76 codes at 1300, 42 at 1500, 18 of them at both.

Read through ``n_eban.requisitioner`` (see :mod:`app.shared.sap_normalise`).
Values that are not 5 digits (``BMW``, ``OAR``, ``GINCO``) are purchasing desks
and people, not departments, and are left out. Codes only: no extract names
them, and a made-up name is worse than a code.

Which plant a code belongs to
-----------------------------
A code is listed under every plant it has a requisition at. The opener shows
the picked plant's codes; the others are still offered on search, because a
department raising its first requisition at the other site is ordinary. When a
plant has no codes at all, every code is offered, rather than an empty list
that would leave the form impossible to complete.

The check when a session starts
-------------------------------
:func:`check` is applied by ``POST /api/assistant/sessions``:

* blank -- allowed; the API has always treated department as optional;
* opened from SAP (``BADI``) -- stored as sent, with a log line if it is not a
  known code. The SAP link is not ours to fail;
* opened on the platform -- must be 5 digits, and must be on the list once the
  list has loaded. Before it has (or if it cannot be read), 5 digits is enough:
  the opener must not stop working because a background load is slow.

``ASSISTANT_DEPARTMENT_CHECK_ENABLED=false`` turns the check off entirely.

Loaded whole, in the background, the same way as
:mod:`app.shared.material_names`, and reloaded after :data:`CACHE_SECONDS`, so a
department's first requisition reaches the list within hours without any hook
on the loaders.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.shared.plant_scope import IN_SCOPE_PLANTS, normalise as normalise_plant, sql_literals

logger = get_logger(__name__)

#: A department code: exactly five digits.
CODE = re.compile(r"\d{5}")

#: How long a loaded list is trusted before it is read again.
CACHE_SECONDS = 6 * 60 * 60

#: How soon a load that could not read the source is tried again.
RETRY_SECONDS = 10 * 60

#: How long a request waits for the first load right after a restart.
FIRST_LOAD_WAIT_SECONDS = 10

_lock = threading.Lock()
_codes: dict[str, frozenset[str]] = {}
"""``{code: plants it has a requisition at}``."""
_next_load_at = 0.0
_loading: threading.Thread | None = None


class DepartmentError(ValueError):
    """A department the platform opener should not have sent."""


@dataclass(frozen=True)
class Department:
    code: str
    plants: tuple[str, ...]
    """Every in-scope plant with a requisition from this department."""
    primary: bool
    """Shown before anything is typed: used at the asked-for plant, or that
    plant has no codes at all."""


def is_code(value: str | None) -> bool:
    return bool(value) and CODE.fullmatch(value.strip()) is not None


def for_plant(plant: str | None, *, wait_seconds: float = 0) -> list[Department]:
    """Every code, the asked-for plant's first, then the rest by code."""
    if wait_seconds > 0:
        wait_for_first_load(wait_seconds)
    codes = _current()
    plant = normalise_plant(plant)
    has_own = plant is not None and any(plant in plants for plants in codes.values())
    listed = [
        Department(
            code=code,
            plants=tuple(p for p in IN_SCOPE_PLANTS if p in plants),
            primary=(plant in plants) if has_own else True,
        )
        for code, plants in codes.items()
    ]
    return sorted(listed, key=lambda d: (not d.primary, d.code))


def is_loaded() -> bool:
    with _lock:
        return bool(_codes)


def check(value: str | None, *, from_sap: bool, enabled: bool = True) -> None:
    """Raise :class:`DepartmentError` for a value the platform opener may not send.

    ``value`` is already trimmed; blank means "not stated" and always passes.
    """
    if not value or not enabled:
        return
    if from_sap:
        if not (is_code(value) and _known(value)):
            logger.info("assistant: SAP sent department %r, not a known code; stored as sent", value)
        return
    if not is_code(value):
        raise DepartmentError(
            f"Department {value!r} is not a department code. Pick one from the list "
            "(five digits, as on a purchase requisition's Requisitioner field)."
        )
    if not _known(value):
        raise DepartmentError(
            f"Department {value} has no purchase requisition at plant "
            f"{' or '.join(IN_SCOPE_PLANTS)}. Pick one from the list."
        )


def _known(code: str) -> bool:
    """On the list -- or no list yet, in which case nothing can be ruled out."""
    wait_for_first_load(FIRST_LOAD_WAIT_SECONDS)
    codes = _current()
    return not codes or code.strip() in codes


def load_all(db: Session) -> tuple[dict[str, frozenset[str]], bool]:
    """Every department code on a requisition at an in-scope plant."""
    try:
        rows = db.execute(
            text(
                "SELECT DISTINCT plant, requisitioner FROM n_eban "
                f"WHERE plant IN ({sql_literals()}) AND requisitioner IS NOT NULL"
            )
        ).all()
    except DBAPIError as error:
        logger.info("departments: n_eban not readable (%s)", error.__class__.__name__)
        db.rollback()
        return {}, False

    found: dict[str, set[str]] = {}
    for plant, value in rows:
        code = (value or "").strip()
        plant = normalise_plant(plant)
        if plant and CODE.fullmatch(code):
            found.setdefault(code, set()).add(plant)
    return {code: frozenset(plants) for code, plants in found.items()}, True


def start_loading() -> None:
    """Start a background load now if none is loaded or running. For start-up."""
    _ensure_loading()


def wait_for_first_load(timeout: float) -> bool:
    """Wait up to ``timeout`` seconds while nothing is loaded and a load runs."""
    _ensure_loading()
    with _lock:
        if _codes:
            return True
        loading = _loading
    if loading is not None:
        loading.join(timeout)
    return is_loaded()


def wait_for_load(timeout: float | None = None) -> None:
    """Block until a running background load ends. For tests and scripts."""
    with _lock:
        loading = _loading
    if loading is not None:
        loading.join(timeout)


def clear_cache() -> None:
    """For tests: forget the list, so the next call loads again."""
    global _codes, _next_load_at
    wait_for_load()
    with _lock:
        _codes = {}
        _next_load_at = 0.0


def _current() -> dict[str, frozenset[str]]:
    _ensure_loading()
    with _lock:
        return _codes


def _ensure_loading() -> None:
    global _loading
    with _lock:
        if time.monotonic() < _next_load_at or _loading is not None:
            return
        _loading = threading.Thread(target=_load, name="assistant-departments", daemon=True)
        _loading.start()


def _load() -> None:
    global _codes, _next_load_at, _loading
    # Imported here: the session factory reads settings, which tests and
    # scripts may set after this module is imported.
    from app.core.db import get_sessionmaker

    started = time.monotonic()
    retry_in: float = RETRY_SECONDS
    try:
        with get_sessionmaker()() as db:
            codes, complete = load_all(db)
        with _lock:
            # Keep the previous list rather than replacing it with nothing.
            if codes or not _codes:
                _codes = codes
        retry_in = CACHE_SECONDS if complete and codes else RETRY_SECONDS
        logger.info(
            "departments: %d codes loaded in %.1fs%s",
            len(codes), time.monotonic() - started, "" if complete else " (n_eban unreadable)",
        )
    except Exception:  # noqa: BLE001 -- a failed load must never reach a request
        logger.exception("departments: load failed; the opener checks the code's shape only")
    finally:
        with _lock:
            _next_load_at = time.monotonic() + retry_in
            _loading = None


if __name__ == "__main__":  # python -m app.assistant.departments
    from app.core.db import get_sessionmaker

    with get_sessionmaker()() as session:
        loaded, ok = load_all(session)
    for plant in IN_SCOPE_PLANTS:
        mine = sorted(c for c, plants in loaded.items() if plant in plants)
        print(f"{plant}: {len(mine)} codes  {' '.join(mine)}")
    print(f"total {len(loaded)} distinct{'' if ok else '  (n_eban unreadable)'}")
