"""i13_quantity_suggestion

W7.4: reservation-time quantity suggestions (FR-3) and the justifications
requesters give for keeping a different quantity.

Not a mart: these rows record what was put in front of a person and what
they decided, so they are inserted once and updated in place (see
app/models/i13_quantity_suggestion.py). Portable constructs only -- this
creates identically on Postgres and Azure SQL.

Revision ID: d3b1c7a94e52
Revises: a868f8b8ac1a
Create Date: 2026-09-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3b1c7a94e52'
down_revision: Union[str, Sequence[str], None] = 'a868f8b8ac1a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same fixed-point type every SAP-derived quantity in this schema uses.
_QTY = sa.Numeric(18, 6)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i13_quantity_suggestion',
        sa.Column('suggestion_id', sa.String(length=40), nullable=False),
        sa.Column('session_id', sa.String(length=40), nullable=True),
        sa.Column('material', sa.String(length=40), nullable=False),
        sa.Column('plant', sa.String(length=10), nullable=False),
        sa.Column('reservation_number', sa.String(length=20), nullable=True),
        sa.Column('reservation_item', sa.String(length=10), nullable=True),
        sa.Column('requester_id', sa.String(length=40), nullable=True),
        sa.Column('requested_quantity', _QTY, nullable=False),
        # NULL where the engine declined -- never the requested quantity
        # echoed back, which would read as agreement.
        sa.Column('suggested_quantity', _QTY, nullable=True),
        sa.Column('direction', sa.String(length=16), nullable=False),
        sa.Column('reason_code', sa.String(length=32), nullable=False),
        sa.Column('reason_text', sa.String(length=2000), nullable=False),
        sa.Column('reason_source', sa.String(length=16), nullable=False),
        sa.Column('reason_prompt_id', sa.String(length=60), nullable=True),
        sa.Column('reason_prompt_version', sa.Integer(), nullable=True),
        sa.Column('reason_model', sa.String(length=80), nullable=True),
        sa.Column('average_monthly_consumption', _QTY, nullable=False),
        sa.Column('stock_on_hand', _QTY, nullable=False),
        sa.Column('open_po_quantity', _QTY, nullable=False),
        sa.Column('consumption_count_12m', sa.Integer(), nullable=False),
        sa.Column('plan_window_months', _QTY, nullable=False),
        sa.Column('resulting_cover_months', _QTY, nullable=True),
        sa.Column('plan_need_quantity', _QTY, nullable=True),
        sa.Column('net_need_quantity', _QTY, nullable=True),
        sa.Column('ceiling_quantity', _QTY, nullable=True),
        # Config snapshot: what the thresholds WERE for this suggestion.
        sa.Column('cover_ceiling_months', _QTY, nullable=True),
        sa.Column('minimum_history_count', sa.Integer(), nullable=True),
        sa.Column('lookback_months', sa.Integer(), nullable=False),
        # NULL means undecided, which is not the same as rejected (FRS §8).
        sa.Column('accepted', sa.Boolean(), nullable=True),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('accepted_by', sa.String(length=40), nullable=True),
        sa.Column('calculated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('suggestion_id', name=op.f('pk_i13_quantity_suggestion')),
    )
    op.create_index(op.f('ix_i13_quantity_suggestion_session_id'), 'i13_quantity_suggestion', ['session_id'], unique=False)
    op.create_index(op.f('ix_i13_quantity_suggestion_material'), 'i13_quantity_suggestion', ['material'], unique=False)
    op.create_index(op.f('ix_i13_quantity_suggestion_plant'), 'i13_quantity_suggestion', ['plant'], unique=False)
    op.create_index(
        op.f('ix_i13_quantity_suggestion_reservation_number'), 'i13_quantity_suggestion', ['reservation_number'], unique=False
    )
    op.create_index(
        op.f('ix_i13_quantity_suggestion_requester_id'), 'i13_quantity_suggestion', ['requester_id'], unique=False
    )
    op.create_index(op.f('ix_i13_quantity_suggestion_direction'), 'i13_quantity_suggestion', ['direction'], unique=False)
    op.create_index(op.f('ix_i13_quantity_suggestion_reason_code'), 'i13_quantity_suggestion', ['reason_code'], unique=False)
    op.create_index(op.f('ix_i13_quantity_suggestion_accepted'), 'i13_quantity_suggestion', ['accepted'], unique=False)

    op.create_table(
        'i13_quantity_justification',
        sa.Column('justification_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('suggestion_id', sa.String(length=40), nullable=False),
        sa.Column('reason_category', sa.String(length=60), nullable=False),
        sa.Column('free_text', sa.String(length=2000), nullable=False),
        sa.Column('actor_id', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['suggestion_id'],
            ['i13_quantity_suggestion.suggestion_id'],
            name=op.f('fk_i13_quantity_justification_suggestion_id_i13_quantity_suggestion'),
        ),
        sa.PrimaryKeyConstraint('justification_id', name=op.f('pk_i13_quantity_justification')),
    )
    op.create_index(
        op.f('ix_i13_quantity_justification_suggestion_id'), 'i13_quantity_justification', ['suggestion_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_i13_quantity_justification_suggestion_id'), table_name='i13_quantity_justification')
    op.drop_table('i13_quantity_justification')

    op.drop_index(op.f('ix_i13_quantity_suggestion_accepted'), table_name='i13_quantity_suggestion')
    op.drop_index(op.f('ix_i13_quantity_suggestion_reason_code'), table_name='i13_quantity_suggestion')
    op.drop_index(op.f('ix_i13_quantity_suggestion_direction'), table_name='i13_quantity_suggestion')
    op.drop_index(op.f('ix_i13_quantity_suggestion_requester_id'), table_name='i13_quantity_suggestion')
    op.drop_index(op.f('ix_i13_quantity_suggestion_reservation_number'), table_name='i13_quantity_suggestion')
    op.drop_index(op.f('ix_i13_quantity_suggestion_plant'), table_name='i13_quantity_suggestion')
    op.drop_index(op.f('ix_i13_quantity_suggestion_material'), table_name='i13_quantity_suggestion')
    op.drop_index(op.f('ix_i13_quantity_suggestion_session_id'), table_name='i13_quantity_suggestion')
    op.drop_table('i13_quantity_suggestion')
