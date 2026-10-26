"""i7_kpi_formula_version: editable Overview KPI formulas

Revision ID: d2f6b8a41c93
Revises: e7b3d9a25c61
Create Date: 2026-10-26

One row per Save & Apply of the Overview KPI formulas. A new table only -- nothing existing changes, and
with no row in it the Overview uses the formulas it always did.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f6b8a41c93"
down_revision: Union[str, Sequence[str], None] = "e7b3d9a25c61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "i7_kpi_formula_version",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("rebuild_needed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rows_built", sa.Integer(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index("ix_i7_kpi_formula_version_status", "i7_kpi_formula_version", ["status"])


def downgrade() -> None:
    op.drop_index("ix_i7_kpi_formula_version_status", table_name="i7_kpi_formula_version")
    op.drop_table("i7_kpi_formula_version")
