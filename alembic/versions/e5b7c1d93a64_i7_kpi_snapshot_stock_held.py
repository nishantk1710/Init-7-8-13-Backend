"""i7_kpi_snapshot: stock held (inventory value, safety stock value, below reorder point)

Four nullable columns on ``i7_kpi_snapshot`` so a month's snapshot can also say what stock was held, valued in ZAR:
total inventory value, safety stock value, and how many material-plants were below their reorder point (with how many
could be judged).

Nothing is back-filled. Stock on hand is only known as it is now, so a snapshot taken before this change keeps NULL in
all four -- "not measured" -- and a past month is never given a figure from today's stock. Only snapshots captured after
this change carry them. Existing rows, their fingerprints and their log are not touched.

NOT APPLIED ANYWHERE by this change. Apply it on purpose (``alembic upgrade head`` from the SSH console, or
RUN_MIGRATIONS_ON_STARTUP=true) after the deploy. Rollback: ``alembic downgrade -1`` drops the four columns (and with
them any stock figures captured, which cannot be rebuilt for a past month).

Portable constructs only -- this applies identically on Postgres and Azure SQL.

Revision ID: e5b7c1d93a64
Revises: c4e8a2f61d93
Create Date: 2026-11-03 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b7c1d93a64'
down_revision: Union[str, Sequence[str], None] = 'c4e8a2f61d93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('i7_kpi_snapshot', sa.Column('inventory_value', sa.Numeric(precision=24, scale=4), nullable=True))
    op.add_column('i7_kpi_snapshot', sa.Column('safety_stock_value', sa.Numeric(precision=24, scale=4), nullable=True))
    op.add_column('i7_kpi_snapshot', sa.Column('below_reorder_count', sa.Integer(), nullable=True))
    op.add_column('i7_kpi_snapshot', sa.Column('below_reorder_judged', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_kpi_snapshot', 'below_reorder_judged')
    op.drop_column('i7_kpi_snapshot', 'below_reorder_count')
    op.drop_column('i7_kpi_snapshot', 'safety_stock_value')
    op.drop_column('i7_kpi_snapshot', 'inventory_value')
