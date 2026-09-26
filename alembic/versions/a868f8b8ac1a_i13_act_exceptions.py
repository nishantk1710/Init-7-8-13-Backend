"""i13_act_exceptions

Revision ID: a868f8b8ac1a
Revises: 7a51d926c88c
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a868f8b8ac1a'
down_revision: Union[str, Sequence[str], None] = '7a51d926c88c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i13_act_exception',
        sa.Column('exception_id', sa.String(length=120), nullable=False),
        sa.Column('exception_type', sa.String(length=24), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('material', sa.String(length=40), nullable=False),
        sa.Column('plant', sa.String(length=10), nullable=False),
        sa.Column('reservation_number', sa.String(length=20), nullable=True),
        sa.Column('reservation_item', sa.String(length=10), nullable=True),
        sa.Column('session_id', sa.String(length=40), nullable=True),
        sa.Column('ledger_entry_id', sa.String(length=80), nullable=True),
        sa.Column('owner_requester_id', sa.String(length=40), nullable=True),
        sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('requester_due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('current_assignee_type', sa.String(length=16), nullable=True),
        sa.Column('current_assignee_id', sa.String(length=40), nullable=True),
        sa.Column('routing_status', sa.String(length=24), nullable=True),
        sa.Column('reason', sa.String(length=400), nullable=False),
        sa.Column('evidence_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('exception_id', name=op.f('pk_i13_act_exception')),
    )
    op.create_index(op.f('ix_i13_act_exception_exception_type'), 'i13_act_exception', ['exception_type'], unique=False)
    op.create_index(op.f('ix_i13_act_exception_status'), 'i13_act_exception', ['status'], unique=False)
    op.create_index(op.f('ix_i13_act_exception_material'), 'i13_act_exception', ['material'], unique=False)
    op.create_index(op.f('ix_i13_act_exception_plant'), 'i13_act_exception', ['plant'], unique=False)
    op.create_index(
        op.f('ix_i13_act_exception_reservation_number'), 'i13_act_exception', ['reservation_number'], unique=False
    )
    op.create_index(
        op.f('ix_i13_act_exception_owner_requester_id'), 'i13_act_exception', ['owner_requester_id'], unique=False
    )

    op.create_table(
        'i13_act_exception_event',
        sa.Column('event_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('exception_id', sa.String(length=120), nullable=False),
        sa.Column('event_type', sa.String(length=32), nullable=False),
        sa.Column('from_status', sa.String(length=24), nullable=True),
        sa.Column('to_status', sa.String(length=24), nullable=True),
        sa.Column('actor_id', sa.String(length=40), nullable=True),
        sa.Column('actor_type', sa.String(length=16), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata_json', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ['exception_id'], ['i13_act_exception.exception_id'], name=op.f('fk_i13_act_exception_event_exception_id_i13_act_exception')
        ),
        sa.PrimaryKeyConstraint('event_id', name=op.f('pk_i13_act_exception_event')),
    )
    op.create_index(
        op.f('ix_i13_act_exception_event_exception_id'), 'i13_act_exception_event', ['exception_id'], unique=False
    )
    op.create_index(op.f('ix_i13_act_exception_event_timestamp'), 'i13_act_exception_event', ['timestamp'], unique=False)

    op.create_table(
        'i13_act_confirmation',
        sa.Column('confirmation_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('exception_id', sa.String(length=120), nullable=False),
        sa.Column('reason_category', sa.String(length=60), nullable=False),
        sa.Column('free_text', sa.String(length=2000), nullable=False),
        sa.Column('actor_id', sa.String(length=40), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['exception_id'], ['i13_act_exception.exception_id'], name=op.f('fk_i13_act_confirmation_exception_id_i13_act_exception')
        ),
        sa.PrimaryKeyConstraint('confirmation_id', name=op.f('pk_i13_act_confirmation')),
    )
    op.create_index(op.f('ix_i13_act_confirmation_exception_id'), 'i13_act_confirmation', ['exception_id'], unique=False)

    op.create_table(
        'i13_act_notification',
        sa.Column('notification_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('exception_id', sa.String(length=120), nullable=False),
        sa.Column('channel', sa.String(length=16), nullable=False),
        sa.Column('recipient', sa.String(length=120), nullable=True),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.String(length=400), nullable=False),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['exception_id'], ['i13_act_exception.exception_id'], name=op.f('fk_i13_act_notification_exception_id_i13_act_exception')
        ),
        sa.PrimaryKeyConstraint('notification_id', name=op.f('pk_i13_act_notification')),
    )
    op.create_index(op.f('ix_i13_act_notification_exception_id'), 'i13_act_notification', ['exception_id'], unique=False)
    op.create_index(op.f('ix_i13_act_notification_outcome'), 'i13_act_notification', ['outcome'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_i13_act_notification_outcome'), table_name='i13_act_notification')
    op.drop_index(op.f('ix_i13_act_notification_exception_id'), table_name='i13_act_notification')
    op.drop_table('i13_act_notification')

    op.drop_index(op.f('ix_i13_act_confirmation_exception_id'), table_name='i13_act_confirmation')
    op.drop_table('i13_act_confirmation')

    op.drop_index(op.f('ix_i13_act_exception_event_timestamp'), table_name='i13_act_exception_event')
    op.drop_index(op.f('ix_i13_act_exception_event_exception_id'), table_name='i13_act_exception_event')
    op.drop_table('i13_act_exception_event')

    op.drop_index(op.f('ix_i13_act_exception_owner_requester_id'), table_name='i13_act_exception')
    op.drop_index(op.f('ix_i13_act_exception_reservation_number'), table_name='i13_act_exception')
    op.drop_index(op.f('ix_i13_act_exception_plant'), table_name='i13_act_exception')
    op.drop_index(op.f('ix_i13_act_exception_material'), table_name='i13_act_exception')
    op.drop_index(op.f('ix_i13_act_exception_status'), table_name='i13_act_exception')
    op.drop_index(op.f('ix_i13_act_exception_exception_type'), table_name='i13_act_exception')
    op.drop_table('i13_act_exception')
