"""The department codes the assistant offers, their names, and the check behind them.

Where the codes come from
-------------------------
VZI's department list, ``Plant Dept Codes 1.XLSX``: plant, 5-digit department
code and its name (``1300  10100  FINANCE-COMMERCIAL DEPARTMENT``). There is no
SAP pull for it yet, so the workbook is loaded into ``raw_dept_codes`` and read
through ``n_dept_codes`` (see :mod:`app.shared.sap_normalise`). When the pull
lands it fills the same table, and nothing here changes.

Rows whose code is not 5 digits (``BMW``, ``OAR``, ``GINCO``, ``POD``) are MRP
and inventory-controller desks, not departments, and are left out. Until 21 Oct
2026 the codes came from requisitions (``EBAN.AFNAM``) instead, without names.

Which plant a code belongs to
-----------------------------
A code is listed under the plant(s) the list gives it. That only decides what
the screen shows before anything is typed. Departments order parts at the other
site too (``10701`` is listed at 1500 and raises requisitions at 1300), so every
code is offered on search and passes the check whatever the material's plant.
When a plant has no codes at all, every code is offered, rather than an empty
list that would leave the form impossible to complete.

A name belongs to the code, not to the plant. A code listed without a name is
shown as the code alone: a made-up name is worse than a code.

The check when a session starts
-------------------------------
:func:`check` is applied by ``POST /api/assistant/sessions`` and by request
submit:

* blank -- allowed from SAP; the request screen insists on one itself;
* opened from SAP (``BADI``) -- stored as sent, with a log line if it is not a
  known code. The SAP link is not ours to fail;
* opened on the platform -- must be 5 digits, and must be on the list once the
  list has loaded. Before it has (or if it cannot be read), 5 digits is enough:
  the screen must not stop working because a background load is slow.

Anything a requester needs to add about the department goes in the separate,
free-text department note (``assistant_session.department_note``), which is
never checked against this list.

``ASSISTANT_DEPARTMENT_CHECK_ENABLED=false`` turns the check off entirely.

Loaded whole, in the background, the same way as
:mod:`app.shared.material_names`, and reloaded after :data:`CACHE_SECONDS`, so a
reloaded list reaches the screens within hours without any hook on the loaders.
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
"""``{code: plants the list gives it}``."""
_names: dict[str, str] = {}
"""``{code: name}`` for the codes the list names."""
_next_load_at = 0.0
_loading: threading.Thread | None = None


class DepartmentError(ValueError):
    """A department the platform screen should not have sent."""


@dataclass(frozen=True)
class Department:
    code: str
    plants: tuple[str, ...]
    """Every in-scope plant the list gives this code."""
    primary: bool
    """Shown before anything is typed: listed at the asked-for plant, or that
    plant has no codes at all."""
    name: str | None = None
    """The department's name; None when the list gives it none."""


def is_code(value: str | None) -> bool:
    return bool(value) and CODE.fullmatch(value.strip()) is not None


def for_plant(plant: str | None, *, wait_seconds: float = 0) -> list[Department]:
    """Every code, the asked-for plant's first, then the rest by code."""
    if wait_seconds > 0:
        wait_for_first_load(wait_seconds)
    codes, names = _current()
    plant = normalise_plant(plant)
    has_own = plant is not None and any(plant in plants for plants in codes.values())
    listed = [
        Department(
            code=code,
            plants=tuple(p for p in IN_SCOPE_PLANTS if p in plants),
            primary=(plant in plants) if has_own else True,
            name=names.get(code),
        )
        for code, plants in codes.items()
    ]
    return sorted(listed, key=lambda d: (not d.primary, d.code))


def name_for(code: str | None) -> str | None:
    """The name of a department code, or None (unnamed, unknown, not loaded).

    Never waits for a load: it labels rows already written, and a row shown
    without its name is better than a slow page."""
    if not code:
        return None
    with _lock:
        return _names.get(code.strip())


def is_loaded() -> bool:
    with _lock:
        return bool(_codes)


def check(value: str | None, *, from_sap: bool, enabled: bool = True) -> None:
    """Raise :class:`DepartmentError` for a value the platform screen may not send.

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
            "(five digits, e.g. 10100); anything else goes in the department note."
        )
    if not _known(value):
        raise DepartmentError(
            f"Department {value} is not on the department list for plant "
            f"{' or '.join(IN_SCOPE_PLANTS)}. Pick one from the list."
        )


def _known(code: str) -> bool:
    """On the list -- or no list yet, in which case nothing can be ruled out."""
    wait_for_first_load(FIRST_LOAD_WAIT_SECONDS)
    codes, _ = _current()
    return not codes or code.strip() in codes


def load_all(db: Session) -> tuple[dict[str, frozenset[str]], dict[str, str], bool]:
    """Every department code on the list at an in-scope plant, and its name.

    Returns ``(codes, names, complete)``; ``complete`` is False when the list
    could not be read at all.
    """
    try:
        rows = db.execute(
            text(
                "SELECT plant, code, name FROM n_dept_codes "
                f"WHERE plant IN ({sql_literals()}) AND code IS NOT NULL"
            )
        ).all()
    except DBAPIError as error:
        logger.info("departments: n_dept_codes not readable (%s)", error.__class__.__name__)
        db.rollback()
        return {}, {}, False

    found: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for plant, value, name in rows:
        code = (value or "").strip()
        plant = normalise_plant(plant)
        if not (plant and CODE.fullmatch(code)):
            continue
        found.setdefault(code, set()).add(plant)
        name = (name or "").strip()
        if name and code not in names:
            names[code] = name
    return {code: frozenset(plants) for code, plants in found.items()}, names, True


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
    global _codes, _names, _next_load_at
    wait_for_load()
    with _lock:
        _codes = {}
        _names = {}
        _next_load_at = 0.0


def _current() -> tuple[dict[str, frozenset[str]], dict[str, str]]:
    _ensure_loading()
    with _lock:
        return _codes, _names


def _ensure_loading() -> None:
    global _loading
    with _lock:
        if time.monotonic() < _next_load_at or _loading is not None:
            return
        _loading = threading.Thread(target=_load, name="assistant-departments", daemon=True)
        _loading.start()


def _load() -> None:
    global _codes, _names, _next_load_at, _loading
    # Imported here: the session factory reads settings, which tests and
    # scripts may set after this module is imported.
    from app.core.db import get_sessionmaker

    started = time.monotonic()
    retry_in: float = RETRY_SECONDS
    try:
        with get_sessionmaker()() as db:
            codes, names, complete = load_all(db)
        with _lock:
            # Keep the previous list rather than replacing it with nothing.
            if codes or not _codes:
                _codes = codes
                _names = names
        retry_in = CACHE_SECONDS if complete and codes else RETRY_SECONDS
        logger.info(
            "departments: %d codes (%d named) loaded in %.1fs%s",
            len(codes), len(names), time.monotonic() - started,
            "" if complete else " (n_dept_codes unreadable)",
        )
    except Exception:  # noqa: BLE001 -- a failed load must never reach a request
        logger.exception("departments: load failed; the screens check the code's shape only")
    finally:
        with _lock:
            _next_load_at = time.monotonic() + retry_in
            _loading = None


if __name__ == "__main__":  # python -m app.assistant.departments
    from app.core.db import get_sessionmaker

    with get_sessionmaker()() as session:
        loaded, named, ok = load_all(session)
    for plant in IN_SCOPE_PLANTS:
        mine = sorted(c for c, plants in loaded.items() if plant in plants)
        print(f"{plant}: {len(mine)} codes")
        for code in mine:
            print(f"  {code}  {named.get(code, '(no name)')}")
    print(f"total {len(loaded)} distinct, {len(named)} named{'' if ok else '  (n_dept_codes unreadable)'}")
