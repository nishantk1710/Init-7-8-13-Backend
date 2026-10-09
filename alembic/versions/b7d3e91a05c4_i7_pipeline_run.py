"""I07 pipeline run table

Phase B of the I07 refresh-automation work. One new table, nothing altered:
``i7_pipeline_run`` records one end-to-end execution (staging through
recommendations) and points at the stage runs it produced.

Purely additive -- no existing table is touched, so an upgrade cannot affect
anything already running, and ``downgrade`` drops the table whole. The loss on
rollback is the execution history itself; every stage run it points at lives in
its own table and survives.

Indexes are the three questions actually asked of this table: "what is the
current state?" (status), "what has run recently?" (started_at), and "which
pipeline produced this staging run?" (staging_run_id).

Revision ID: b7d3e91a05c4
Revises: a4f1c8e27b63
Create Date: 2026-10-09 00:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b7d3e91a05c4"
down_revision: Union[str, Sequence[str], None] = "a4f1c8e27b63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "i7_pipeline_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trigger_reason", sa.String(length=128), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=512), nullable=True),
        sa.Column(
            "snapshot_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("staging_run_id", sa.Integer(), nullable=True),
        sa.Column("feature_run_id", sa.Integer(), nullable=True),
        sa.Column("forecast_run_id", sa.Integer(), nullable=True),
        sa.Column("inventory_run_id", sa.Integer(), nullable=True),
        sa.Column("oar_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "recommendations_written",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("stage_statuses", sa.Text(), nullable=True),
        sa.Column("failed_stage", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_i7_pipeline_run_status", "i7_pipeline_run", ["status"])
    op.create_index("ix_i7_pipeline_run_started_at", "i7_pipeline_run", ["started_at"])
    op.create_index(
        "ix_i7_pipeline_run_staging_run_id", "i7_pipeline_run", ["staging_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_i7_pipeline_run_staging_run_id", table_name="i7_pipeline_run")
    op.drop_index("ix_i7_pipeline_run_started_at", table_name="i7_pipeline_run")
    op.drop_index("ix_i7_pipeline_run_status", table_name="i7_pipeline_run")
    op.drop_table("i7_pipeline_run")
