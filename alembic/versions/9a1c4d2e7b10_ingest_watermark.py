"""ingest_watermark

Revision ID: 9a1c4d2e7b10
Revises: 770e63ee2f68
Create Date: 2026-09-21 11:05:00.000000

Delta pulls need somewhere to remember how far they got. See
``app/models/ingest_watermark.py`` for why this is separate from
``ingestion_run`` and why the value is text.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a1c4d2e7b10'
down_revision: Union[str, Sequence[str], None] = '770e63ee2f68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ingest_watermark',
        sa.Column('entity_set', sa.String(length=128), nullable=False),
        sa.Column('field', sa.String(length=64), nullable=False),
        sa.Column('value', sa.String(length=64), nullable=False),
        sa.Column('rows_last_run', sa.Integer(), nullable=False),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('entity_set', name=op.f('pk_ingest_watermark')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('ingest_watermark')
