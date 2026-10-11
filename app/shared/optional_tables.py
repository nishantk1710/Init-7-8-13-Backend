"""Which optional raw tables exist -- so a reader can treat "not loaded" as empty.

Some raw tables are not part of the SAP CSV pull and may simply not be there:
the ZMM065 aging reports (``raw_zmm065_bmm``, ``raw_zmm065_gb``) are workbooks
shared outside SAP, loaded by hand with ``app.ingest.fallback``. A reader that
assumes them fails on ``Invalid object name`` -- and on 10 Oct 2026 that one
missing report stopped the whole I13 build and every I08 view. Readers ask here
first and leave a missing table out, the way the ``n_*`` views already build
empty over an SAP table that was never loaded.

Always asked fresh, never cached: a table can be loaded while the API runs.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

_EXISTING = text("SELECT name FROM sys.tables WHERE name IN :names").bindparams(
    bindparam("names", expanding=True)
)


def existing(db: Session | Connection, names: Iterable[str]) -> tuple[str, ...]:
    """``names`` that exist as tables, in the order given."""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return ()
    found = {row[0].lower() for row in db.execute(_EXISTING, {"names": wanted})}
    return tuple(name for name in wanted if name.lower() in found)
