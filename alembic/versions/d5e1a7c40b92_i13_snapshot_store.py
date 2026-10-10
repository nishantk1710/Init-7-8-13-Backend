"""I13 snapshot store: i13_snapshot_run and i13_snapshot_record

The I13 snapshot moves out of the API process into Azure SQL when
``I13_SNAPSHOT_STORE=sql`` (see app/models/i13_snapshot_store.py for why). Two
new tables; nothing existing changes, and with the setting left at its default
("memory") neither is written or read.

``i13_snapshot_record`` is the large one -- a few million rows per version on a
production extract, briefly two versions while a new one replaces the old.
Every index leads with (version, kind): every read names both.

ROLLBACK drops both tables. With the store set back to "memory" nothing reads
them, so a downgrade loses only the stored snapshot, which the next in-memory
build replaces.

Revision ID: d5e1a7c40b92
Revises: c2a8f05d31e7
Create Date: 2026-10-10 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5e1a7c40b92"
down_revision: str | None = "c2a8f05d31e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "i13_snapshot_run",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Unicode(length=80), nullable=True),
        sa.Column("fingerprint", sa.String(length=32), nullable=True),
        sa.Column("schema_signature", sa.String(length=32), nullable=False),
        sa.Column("reference_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("build_seconds", sa.Numeric(12, 1), nullable=True),
        sa.Column("batches", sa.Integer(), nullable=True),
        sa.Column("meta", sa.UnicodeText(), nullable=True),
        sa.Column("error", sa.UnicodeText(), nullable=True),
        sa.PrimaryKeyConstraint("version"),
    )
    op.create_index("ix_i13_snapshot_run_status", "i13_snapshot_run", ["status"])

    op.create_table(
        "i13_snapshot_record",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("material", sa.Unicode(length=40), nullable=False),
        sa.Column("plant", sa.Unicode(length=10), nullable=False),
        sa.Column("rec_key", sa.Unicode(length=100), nullable=True),
        sa.Column("oar", sa.Boolean(), nullable=False),
        sa.Column("f1", sa.Unicode(length=40), nullable=True),
        sa.Column("f2", sa.Unicode(length=40), nullable=True),
        sa.Column("f3", sa.Unicode(length=40), nullable=True),
        sa.Column("n1", sa.Numeric(28, 6), nullable=True),
        sa.Column("d1", sa.Date(), nullable=True),
        sa.Column("text1", sa.Unicode(length=400), nullable=True),
        sa.Column("payload", sa.String(length=8000), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_i13_snaprec_seq", "i13_snapshot_record", ["version", "kind", "seq"])
    op.create_index("ix_i13_snaprec_key", "i13_snapshot_record", ["version", "kind", "material", "plant"])
    op.create_index("ix_i13_snaprec_reckey", "i13_snapshot_record", ["version", "kind", "rec_key"])
    op.create_index("ix_i13_snaprec_f1", "i13_snapshot_record", ["version", "kind", "f1"])
    op.create_index("ix_i13_snaprec_f2", "i13_snapshot_record", ["version", "kind", "f2"])
    op.create_index("ix_i13_snaprec_f3", "i13_snapshot_record", ["version", "kind", "f3"])


def downgrade() -> None:
    op.drop_table("i13_snapshot_record")
    op.drop_index("ix_i13_snapshot_run_status", table_name="i13_snapshot_run")
    op.drop_table("i13_snapshot_run")
