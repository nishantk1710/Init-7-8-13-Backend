"""I08 snapshot store: i8_snapshot_run

The I08 snapshot moves out of the API's web worker into Azure SQL when
``I8_SNAPSHOT_STORE=sql`` (see app/models/i8_snapshot_store.py for why). One
new table; nothing existing changes, and with the setting left at its default
("memory") it is neither written nor read.

ROLLBACK drops the table. With the store set back to "memory" nothing reads it,
so a downgrade loses only the stored snapshot, which the next in-memory build
replaces.

Revision ID: a7d2e94b1c35
Revises: d5e1a7c40b92
Create Date: 2026-10-12 08:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d2e94b1c35"
down_revision: str | None = "d5e1a7c40b92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "i8_snapshot_run",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Unicode(length=80), nullable=True),
        sa.Column("fingerprint", sa.UnicodeText(), nullable=True),
        sa.Column("codec_version", sa.Integer(), nullable=False),
        sa.Column("reference_date", sa.Date(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("build_seconds", sa.Numeric(12, 1), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=True),
        sa.Column("payload_bytes", sa.Integer(), nullable=True),
        sa.Column("error", sa.UnicodeText(), nullable=True),
        sa.PrimaryKeyConstraint("version"),
    )
    op.create_index("ix_i8_snapshot_run_status", "i8_snapshot_run", ["status"])


def downgrade() -> None:
    op.drop_index("ix_i8_snapshot_run_status", table_name="i8_snapshot_run")
    op.drop_table("i8_snapshot_run")
