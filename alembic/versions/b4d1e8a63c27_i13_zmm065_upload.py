"""i13_zmm065_upload and i13_zmm065_upload_row -- monthly ZMM065 uploads for FR-6 validation

Revision ID: b4d1e8a63c27
Revises: 5c9e2a7d4f18
Create Date: 2026-10-09

VZI uploads each month's ZMM065 aging report on the Validation screen; the
validation endpoint reconciles against the latest upload per plant and falls
back to the seeded July workbooks where a plant has none. See
``app/models/i13_zmm065_upload.py``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4d1e8a63c27"
down_revision: Union[str, Sequence[str], None] = "5c9e2a7d4f18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "i13_zmm065_upload",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plant", sa.String(length=10), nullable=False),
        sa.Column("report_month", sa.Date(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("sheet_name", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i13_zmm065_upload")),
    )
    op.create_index(op.f("ix_i13_zmm065_upload_plant"), "i13_zmm065_upload", ["plant"])
    op.create_index(op.f("ix_i13_zmm065_upload_report_month"), "i13_zmm065_upload", ["report_month"])

    op.create_table(
        "i13_zmm065_upload_row",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=False),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=10), nullable=False),
        sa.Column("stock_type", sa.String(length=40), nullable=False),
        sa.Column("last_gi_date", sa.Date(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["i13_zmm065_upload.id"],
            name=op.f("fk_i13_zmm065_upload_row_upload_id_i13_zmm065_upload"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i13_zmm065_upload_row")),
    )
    op.create_index(op.f("ix_i13_zmm065_upload_row_upload_id"), "i13_zmm065_upload_row", ["upload_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_i13_zmm065_upload_row_upload_id"), table_name="i13_zmm065_upload_row")
    op.drop_table("i13_zmm065_upload_row")
    op.drop_index(op.f("ix_i13_zmm065_upload_report_month"), table_name="i13_zmm065_upload")
    op.drop_index(op.f("ix_i13_zmm065_upload_plant"), table_name="i13_zmm065_upload")
    op.drop_table("i13_zmm065_upload")
