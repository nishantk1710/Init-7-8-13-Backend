"""i7 recommendation: the months its consumption count covers

Revision ID: f4b8d2a71c63
Revises: e6a3c91d5b28
Create Date: 2026-10-30 12:00:00.000000

``i7_recommendation.consumption_count_12m`` holds the OAR conversion trigger's count, now over 6 months (it
was 12 until 2026-10-22) although the column keeps its old name. ``consumption_count_months`` records which
window a recommendation was counted over, so a screen labels it correctly and a later policy change cannot
relabel a recommendation already made. Nullable: a recommendation made before this column has no recorded
window, and NULL says so.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4b8d2a71c63'
down_revision: Union[str, Sequence[str], None] = 'e6a3c91d5b28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('i7_recommendation', sa.Column('consumption_count_months', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_recommendation', 'consumption_count_months')
