"""I07 staging lineage and soft deactivation

Phase A of the I07 refresh-automation work. Three independent additions, all
additive and all nullable-or-defaulted, so an existing database keeps working
unchanged until the code that reads them runs.

1. LINEAGE. ``i7_staging_run.source_fingerprint`` and
   ``i7_feature_run.staging_run_id`` / ``.source_fingerprint`` make "which
   ingestion refresh produced this feature?" answerable. Nullable on purpose:
   rows written before this migration genuinely do not know which run or load
   they came from, and NULL says that rather than inventing a plausible id.

2. SOFT DEACTIVATION. ``is_active`` on the two tables the feature universe is
   built from. Defaulted to 1 (true) at the server, so every existing row stays
   in scope through the upgrade -- the sweep is what deactivates rows, and it
   has not run yet. NOT NULL, because "we never decided" and "still active" are
   the same thing for a flag whose false state is only ever set deliberately.

3. SWEEP PROVENANCE. ``i7_staging_run.snapshot_complete`` and ``.deactivated``
   record whether a run was entitled to sweep and what it swept. Defaulted to
   0/false: a historical run did not sweep, and must not read as though it had.

ROLLBACK. ``downgrade`` drops all seven columns. That is lossless for the raw
data -- no staged or feature row is touched -- but it discards two things that
cannot be recomputed: the lineage of runs made while this was live, and any
deactivation decisions. After a downgrade every row is in scope again, which is
the pre-migration behaviour and is safe (it over-includes rather than silently
dropping materials), but a re-upgrade starts from a clean slate and needs one
complete-snapshot staging run to re-establish which rows are gone.

SQL Server note: adding a NOT NULL column to a populated table requires a
default, which is why these carry ``server_default`` rather than relying on the
ORM-side ``default=``. The server default is deliberately LEFT IN PLACE after
the backfill: the staging upsert writes column lists that do not always include
``is_active``, and without a server default those inserts would fail.

Revision ID: a4f1c8e27b63
Revises: b4d1e8a63c27
Create Date: 2026-10-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a4f1c8e27b63"
down_revision: Union[str, Sequence[str], None] = "b4d1e8a63c27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Lineage on the staging run ---------------------------------------
    op.add_column(
        "i7_staging_run",
        sa.Column("source_fingerprint", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "i7_staging_run",
        sa.Column(
            "snapshot_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "i7_staging_run",
        sa.Column("deactivated", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # --- Lineage on the feature run ---------------------------------------
    op.add_column(
        "i7_feature_run",
        sa.Column("staging_run_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "i7_feature_run",
        sa.Column("source_fingerprint", sa.String(length=512), nullable=True),
    )
    # Indexed because the question it answers -- "which feature runs came from
    # this staging run?" -- is asked by the orchestrator on every run, and the
    # table grows one row per build forever.
    op.create_index(
        "ix_i7_feature_run_staging_run_id", "i7_feature_run", ["staging_run_id"]
    )

    # --- Soft deactivation on the universe tables -------------------------
    #
    # No explicit backfill: the server default supplies 1 for every existing
    # row as the column is added, which is the correct starting state. Nothing
    # has been observed missing from a snapshot yet, so nothing is inactive.
    for table in ("i7_staged_material_plant", "i7_staged_stock"):
        op.add_column(
            table,
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )


def downgrade() -> None:
    for table in ("i7_staged_material_plant", "i7_staged_stock"):
        op.drop_column(table, "is_active")

    op.drop_index("ix_i7_feature_run_staging_run_id", table_name="i7_feature_run")
    op.drop_column("i7_feature_run", "source_fingerprint")
    op.drop_column("i7_feature_run", "staging_run_id")

    op.drop_column("i7_staging_run", "deactivated")
    op.drop_column("i7_staging_run", "snapshot_complete")
    op.drop_column("i7_staging_run", "source_fingerprint")
