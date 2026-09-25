"""i7 forecast champion columns

Revision ID: 2a7c1f4e9b31
Revises: 1059fe4c7ddd
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a7c1f4e9b31'
down_revision: Union[str, Sequence[str], None] = '1059fe4c7ddd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i7_forecast',
        sa.Column('is_champion', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'i7_forecast', sa.Column('adoption_status', sa.String(length=64), nullable=True)
    )
    op.add_column(
        'i7_forecast', sa.Column('decision_reason', sa.String(length=500), nullable=True)
    )
    op.alter_column('i7_forecast', 'is_champion', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_forecast', 'decision_reason')
    op.drop_column('i7_forecast', 'adoption_status')
    op.drop_column('i7_forecast', 'is_champion')
