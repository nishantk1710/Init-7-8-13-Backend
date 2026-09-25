"""Shared skip conditions for the Initiative 08 tests.

Three tiers, because three different things can be absent and each needs a
different message:

``needs_db``
    DATABASE_URL is unset. Same condition the existing suite uses.

``needs_seed``
    The database is there but the July extracts were never loaded into it. This
    is the normal state in CI -- the ~800 MB delivery will never live there --
    so every test that asserts a measured figure skips rather than fails.

``needs_views``
    Seeded, but the I08 normalise views have not been created. The fix is one
    command, and the skip says so rather than leaving a reader to guess.

Tests that need none of these -- the detection rules, the aging arithmetic, the
status derivations -- are the majority, and they run everywhere.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.config import get_settings


def database_configured() -> bool:
    return bool(get_settings().database_url)


def _seeded() -> bool:
    if not database_configured():
        return False
    try:
        from app.core.db import get_engine

        with get_engine().connect() as connection:
            return bool(
                connection.execute(
                    text("select to_regclass('public.raw_ekpo') is not null")
                ).scalar()
            )
    except Exception:
        return False


def _views_present() -> bool:
    if not _seeded():
        return False
    try:
        from app.initiatives.i8.views import missing_views

        return not missing_views()
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not database_configured(), reason="DATABASE_URL not set"
)

needs_seed = pytest.mark.skipif(
    not _seeded(),
    reason="the SAP extracts are not loaded (run: python -m app.seed --all)",
)

needs_views = pytest.mark.skipif(
    not _views_present(),
    reason=(
        "the I08 normalise views are missing "
        "(run: python -m app.initiatives.i8.views create)"
    ),
)
