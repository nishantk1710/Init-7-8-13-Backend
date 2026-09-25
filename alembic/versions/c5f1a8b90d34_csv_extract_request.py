"""csv_extract_request: one row per CSV extract fired at SAP

The correlation table for the CSV route. SAP's push carries no request id, no
chunk number and no total, so a row here -- written before the request is
fired, and OPEN for exactly one extract at a time -- is the only thing that
can attribute an arriving chunk to what asked for it.

Revision ID: c5f1a8b90d34
Revises: b3e77c50a4f2
Create Date: 2026-09-25

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f1a8b90d34"
down_revision: Union[str, Sequence[str], None] = "b3e77c50a4f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "csv_extract_request",
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("sap_table", sa.String(length=32), nullable=False),
        sa.Column("entity_set", sa.String(length=128), nullable=False),
        sa.Column("from_date", sa.String(length=8), nullable=False),
        sa.Column("to_date", sa.String(length=8), nullable=False),
        sa.Column("max_rows", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reconcile", sa.String(length=16), nullable=False),
        sa.Column("expected_rows", sa.Integer(), nullable=True),
        sa.Column("received_rows", sa.Integer(), nullable=False),
        sa.Column("received_chunks", sa.Integer(), nullable=False),
        # BigInteger: a multi-gigabyte extract overflows INT.
        sa.Column("received_bytes", sa.BigInteger(), nullable=False),
        sa.Column("data_key", sa.String(length=512), nullable=True),
        sa.Column("ack", sa.String(length=512), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_chunk_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    # The receiver looks up the open request on EVERY arriving chunk, and a
    # 50,000-row table arrives as one POST per chunk. Both columns are in that
    # lookup's WHERE clause.
    op.create_index(
        "ix_csv_extract_request_status", "csv_extract_request", ["status"]
    )
    op.create_index(
        "ix_csv_extract_request_sap_table", "csv_extract_request", ["sap_table"]
    )


def downgrade() -> None:
    op.drop_index("ix_csv_extract_request_sap_table", table_name="csv_extract_request")
    op.drop_index("ix_csv_extract_request_status", table_name="csv_extract_request")
    op.drop_table("csv_extract_request")
