"""i7 recommendation structured conversion evidence

Revision ID: 3f8d1c7e6a29
Revises: 9d5e2c8a4b17
Create Date: 2026-09-17 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f8d1c7e6a29'
down_revision: Union[str, Sequence[str], None] = '9d5e2c8a4b17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i7_recommendation', sa.Column('consumption_count_12m', sa.Integer(), nullable=True)
    )
    op.add_column(
        'i7_recommendation',
        sa.Column('consumption_count_threshold', sa.Integer(), nullable=True),
    )
    op.add_column(
        'i7_recommendation', sa.Column('production_impact', sa.Boolean(), nullable=True)
    )
    op.add_column(
        'i7_recommendation', sa.Column('i13_hod_approved', sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_recommendation', 'i13_hod_approved')
    op.drop_column('i7_recommendation', 'production_impact')
    op.drop_column('i7_recommendation', 'consumption_count_threshold')
    op.drop_column('i7_recommendation', 'consumption_count_12m')
