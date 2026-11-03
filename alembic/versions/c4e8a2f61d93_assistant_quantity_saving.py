"""assistant quantity_saving

What the quantity suggestion saved on each OAR request line: the quantity the
requester typed before seeing the suggestion, the suggestion, the number they
locked, units saved, and the value at I07's unit price (MBEW VERPR / PEINH).

A new table rather than columns on ``quantity_suggestion``, which is
append-only and written before the lock is known. Append-only too: on SQL
Server by the same trigger as 5c9e2a7d4f18.

Revision ID: c4e8a2f61d93
Revises: c4d8e2f71a39
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e8a2f61d93"
down_revision: Union[str, Sequence[str], None] = "c4d8e2f71a39"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TRIGGER = """
CREATE TRIGGER quantity_saving_no_update_or_delete
ON quantity_saving
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM deleted)
        THROW 50001,
            'quantity_saving is append-only: a saving records what was asked for and what was locked. Both are evidence; neither is editable.',
            1;
END
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "quantity_saving",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=False),
        sa.Column("required_quantity", sa.Numeric(18, 3), nullable=False),
        sa.Column("suggested_quantity", sa.Numeric(18, 3), nullable=True),
        sa.Column("final_quantity", sa.Numeric(18, 3), nullable=False),
        sa.Column("quantity_choice", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("units_saved", sa.Numeric(18, 3), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("price_valuation_type", sa.String(length=16), nullable=True),
        sa.Column("price_missing_reason", sa.String(length=32), nullable=True),
        sa.Column("value_saved", sa.Numeric(18, 2), nullable=True),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["assistant_session.id"]),
        sa.ForeignKeyConstraint(["request_id"], ["assistant_request.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("session_id", "request_id", "material", "plant", "recorded_by", "recorded_at"):
        op.create_index(f"ix_quantity_saving_{column}", "quantity_saving", [column])
    if op.get_bind().dialect.name == "mssql":
        op.execute(_TRIGGER)


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "mssql":
        op.execute("DROP TRIGGER IF EXISTS quantity_saving_no_update_or_delete")
    for column in ("session_id", "request_id", "material", "plant", "recorded_by", "recorded_at"):
        op.drop_index(f"ix_quantity_saving_{column}", table_name="quantity_saving")
    op.drop_table("quantity_saving")
