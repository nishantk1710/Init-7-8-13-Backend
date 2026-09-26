"""i13_reclassification

Revision ID: 7a51d926c88c
Revises: c4a7f5e6b1d2
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a51d926c88c'
down_revision: Union[str, Sequence[str], None] = 'c4a7f5e6b1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i13_reclassification',
        sa.Column('material', sa.String(length=40), nullable=False),
        sa.Column('plant', sa.String(length=10), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('consumption_count_12m', sa.Integer(), nullable=False),
        sa.Column('consumption_threshold', sa.Integer(), nullable=False),
        sa.Column('consumed_more_than_threshold', sa.Boolean(), nullable=False),
        sa.Column('critical_impact_indicator', sa.Boolean(), nullable=True),
        sa.Column('hod_justified_request_indicator', sa.Boolean(), nullable=True),
        sa.Column('data_available', sa.Boolean(), nullable=False),
        sa.Column('candidate_flag', sa.Boolean(), nullable=False),
        sa.Column('candidate_reasons', sa.String(length=120), nullable=False),
        # CURRENT_TIMESTAMP (ANSI standard), not Postgres's now() -- this
        # table must create identically on Postgres and Azure SQL (see
        # app/models/base.py's portability rule). The application itself
        # never relies on this server_default (reclassification_mart.py
        # always sets refreshed_at explicitly); it exists only as a safety
        # net for any other insert path.
        sa.Column('refreshed_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('material', 'plant', name=op.f('pk_i13_reclassification')),
    )
    op.create_index(
        op.f('ix_i13_reclassification_candidate_flag'), 'i13_reclassification', ['candidate_flag'], unique=False
    )
    op.create_index(
        op.f('ix_i13_reclassification_consumed_more_than_threshold'),
        'i13_reclassification',
        ['consumed_more_than_threshold'],
        unique=False,
    )
    op.create_index(
        op.f('ix_i13_reclassification_data_available'), 'i13_reclassification', ['data_available'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_i13_reclassification_data_available'), table_name='i13_reclassification')
    op.drop_index(op.f('ix_i13_reclassification_consumed_more_than_threshold'), table_name='i13_reclassification')
    op.drop_index(op.f('ix_i13_reclassification_candidate_flag'), table_name='i13_reclassification')
    op.drop_table('i13_reclassification')
