"""csv_extract_request.sweep_id

One nullable column, so a full-refresh sweep is identifiable as ONE event.

WHY THIS IS NEEDED AT ALL

``request_id`` is ``F<TABLE><epoch-seconds>``: unique per REQUEST, because SAP
dedupes on it and a reused id silently delivers nothing. Nothing in it, or in
any other column of this table, records which requests were fired *together*.
The two alternatives were both measured and rejected:

* ``fired_at`` proximity -- a sweep runs ~15 minutes, fires in batches with
  gaps, and can cross midnight. Any window wide enough to hold a real sweep is
  also wide enough to hold a single-table pull fired beside it.
* ``from_date``/``to_date`` -- differ BY TABLE (``windowed`` tables get a
  three-year window, the rest a wide one), so they cannot even group a sweep,
  let alone identify one.

Without this column, "did a complete snapshot land?" could only be guessed, and
guessing it wrongly runs I07's deactivation sweep over a partial refresh --
marking most of the catalogue inactive.

NULLABLE, AND NULL IS A REAL ANSWER

A single-table ``--csv-pull --table EKPO`` is not a sweep and must never read as
one, so it leaves this null on purpose. Rows written before this migration are
null for the same reason and resolve to "no verified sweep", which is the safe
direction: ``snapshot_complete`` stays False and no deactivation runs.

ROLLBACK drops the column. Nothing else reads it, and I07's resolver treats its
absence as "cannot verify" rather than failing -- so a downgrade degrades the
snapshot check to never-complete instead of breaking ingestion.

Revision ID: c2a8f05d31e7
Revises: b7d3e91a05c4
Create Date: 2026-10-09 01:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c2a8f05d31e7"
down_revision: Union[str, Sequence[str], None] = "b7d3e91a05c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "csv_extract_request",
        sa.Column("sweep_id", sa.String(length=32), nullable=True),
    )
    # The resolver's only query shape: every request carrying one sweep id.
    op.create_index(
        "ix_csv_extract_request_sweep_id", "csv_extract_request", ["sweep_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_csv_extract_request_sweep_id", table_name="csv_extract_request")
    op.drop_column("csv_extract_request", "sweep_id")
