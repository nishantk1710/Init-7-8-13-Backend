"""i7 staged consumption issue/reversal counts

Revision ID: 7c3b9a1e5f42
Revises: 2a7c1f4e9b31
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c3b9a1e5f42'
down_revision: Union[str, Sequence[str], None] = '2a7c1f4e9b31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i7_staged_consumption',
        sa.Column('issue_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'i7_staged_consumption',
        sa.Column('reversal_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.alter_column('i7_staged_consumption', 'issue_count', server_default=None)
    op.alter_column('i7_staged_consumption', 'reversal_count', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_staged_consumption', 'reversal_count')
    op.drop_column('i7_staged_consumption', 'issue_count')
