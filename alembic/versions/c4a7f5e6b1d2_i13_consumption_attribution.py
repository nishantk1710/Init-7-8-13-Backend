"""i13_consumption_attribution

Revision ID: c4a7f5e6b1d2
Revises: ba29ede70827
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a7f5e6b1d2'
down_revision: Union[str, Sequence[str], None] = 'ba29ede70827'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i13_consumption_attribution',
        sa.Column('ledger_id', sa.String(length=80), nullable=False),
        sa.Column('material', sa.String(length=40), nullable=False),
        sa.Column('plant', sa.String(length=10), nullable=False),
        sa.Column('reservation_number', sa.String(length=20), nullable=False),
        sa.Column('reservation_item', sa.String(length=10), nullable=False),
        sa.Column('requester_id', sa.String(length=40), nullable=True),
        sa.Column('order_number', sa.String(length=20), nullable=True),
        sa.Column('cost_centre', sa.String(length=20), nullable=True),
        sa.Column('attribution_status', sa.String(length=24), nullable=False),
        sa.Column('attribution_source', sa.String(length=24), nullable=False),
        sa.Column('evidence', sa.String(length=400), nullable=False),
        sa.Column('cost_centre_attribution_enabled', sa.Boolean(), nullable=False),
        # CURRENT_TIMESTAMP (ANSI standard), not Postgres's now() -- this
        # table must create identically on Postgres and Azure SQL (see
        # app/models/base.py's portability rule). The application itself
        # never relies on this server_default (consumption_attribution_mart.py
        # always sets refreshed_at explicitly); it exists only as a safety
        # net for any other insert path.
        sa.Column('refreshed_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('ledger_id', name=op.f('pk_i13_consumption_attribution')),
    )
    op.create_index(
        op.f('ix_i13_consumption_attribution_material'), 'i13_consumption_attribution', ['material'], unique=False
    )
    op.create_index(
        op.f('ix_i13_consumption_attribution_plant'), 'i13_consumption_attribution', ['plant'], unique=False
    )
    op.create_index(
        op.f('ix_i13_consumption_attribution_reservation_number'),
        'i13_consumption_attribution',
        ['reservation_number'],
        unique=False,
    )
    op.create_index(
        op.f('ix_i13_consumption_attribution_attribution_status'),
        'i13_consumption_attribution',
        ['attribution_status'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_i13_consumption_attribution_attribution_status'), table_name='i13_consumption_attribution')
    op.drop_index(op.f('ix_i13_consumption_attribution_reservation_number'), table_name='i13_consumption_attribution')
    op.drop_index(op.f('ix_i13_consumption_attribution_plant'), table_name='i13_consumption_attribution')
    op.drop_index(op.f('ix_i13_consumption_attribution_material'), table_name='i13_consumption_attribution')
    op.drop_table('i13_consumption_attribution')
