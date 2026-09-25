"""i7 recommendation rationale (AI-generated + fallback)

Revision ID: 8b4f1e6a2d93
Revises: 6a2e9f5b3c14
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b4f1e6a2d93'
down_revision: Union[str, Sequence[str], None] = '6a2e9f5b3c14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('i7_recommendation', sa.Column('rationale_text', sa.Text(), nullable=True))
    op.add_column(
        'i7_recommendation', sa.Column('rationale_source', sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_recommendation', 'rationale_source')
    op.drop_column('i7_recommendation', 'rationale_text')
