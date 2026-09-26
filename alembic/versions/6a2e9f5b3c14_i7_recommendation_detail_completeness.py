"""i7 recommendation detail completeness

Revision ID: 6a2e9f5b3c14
Revises: 3f8d1c7e6a29
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a2e9f5b3c14'
down_revision: Union[str, Sequence[str], None] = '3f8d1c7e6a29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('i7_recommendation', sa.Column('circuit', sa.String(length=32), nullable=True))
    op.add_column(
        'i7_recommendation', sa.Column('unit_price', sa.Numeric(18, 4), nullable=True)
    )
    op.add_column(
        'i7_recommendation', sa.Column('lead_time_days', sa.Numeric(18, 6), nullable=True)
    )
    op.add_column(
        'i7_recommendation',
        sa.Column('lead_time_variance_days', sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        'i7_recommendation', sa.Column('service_level', sa.Numeric(18, 6), nullable=True)
    )
    op.add_column(
        'i7_recommendation', sa.Column('z_factor', sa.Numeric(18, 6), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_recommendation', 'z_factor')
    op.drop_column('i7_recommendation', 'service_level')
    op.drop_column('i7_recommendation', 'lead_time_variance_days')
    op.drop_column('i7_recommendation', 'lead_time_days')
    op.drop_column('i7_recommendation', 'unit_price')
    op.drop_column('i7_recommendation', 'circuit')
