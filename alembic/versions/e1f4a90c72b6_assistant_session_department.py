"""assistant_session.department -- who the reservation is being made for

Revision ID: e1f4a90c72b6
Revises: d8c3b1f04e75, d3b1c7a94e52
Create Date: 2026-09-23

Two jobs again, for the same reason the spine migration had two.

1. It adds ``assistant_session.department``.

2. **It is a merge revision.** The spine migration merged ``b2f4c81d5a37`` and
   ``a868f8b8ac1a``, but ``d3b1c7a94e52`` (i13_quantity_suggestion) also
   descends from ``a868f8b8ac1a`` and was never merged in, so the history has
   had two heads ever since:

       Multiple head revisions are present for given argument 'head'

   ``alembic upgrade head`` has therefore been failing on a merged checkout,
   which means this column could not have been applied by adding it to either
   branch alone. Naming both heads as parents is the truth about the
   dependency: the assistant writes ``quantity_suggestion`` rows, so the
   session table and that table were always on one lineage in practice.

Why the column is nullable, and stays nullable
-----------------------------------------------
``assistant_session`` is append-only -- a Postgres trigger blocks UPDATE -- so
existing rows cannot be backfilled, by this migration or by anything else. A
NOT NULL column would need a default, and defaulting a department means
inventing one for every session minted before anybody was asked. The BAdI deep
link does not carry a department either, so NULL remains a legitimate value for
new rows rather than a temporary state to be tightened later.

Nothing is dropped here
------------------------
``requested_quantity`` stops being collected at the entry point in this change,
but the column stays. Sessions minted before it was dropped carry a real value,
and on an append-only audit table a dropped column is history destroyed rather
than a field tidied away.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f4a90c72b6"
down_revision: Union[str, Sequence[str], None] = ("d8c3b1f04e75", "d3b1c7a94e52")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "assistant_session",
        sa.Column("department", sa.String(length=64), nullable=True),
    )
    # Indexed because the adoption question both FRSs ask is per-department --
    # "who is actually using this" -- and that is a filter over the whole log
    # rather than a lookup of one row.
    op.create_index(
        op.f("ix_assistant_session_department"), "assistant_session", ["department"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_assistant_session_department"), table_name="assistant_session")
    op.drop_column("assistant_session", "department")
