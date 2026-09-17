"""i7 material feature consumption_count_12m

Revision ID: 9d5e2c8a4b17
Revises: 7c3b9a1e5f42
Create Date: 2026-09-17 00:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d5e2c8a4b17'
down_revision: Union[str, Sequence[str], None] = '7c3b9a1e5f42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i7_material_feature',
        sa.Column('consumption_count_12m', sa.Integer(), nullable=False, server_default='0'),
    )
    op.alter_column('i7_material_feature', 'consumption_count_12m', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_material_feature', 'consumption_count_12m')
