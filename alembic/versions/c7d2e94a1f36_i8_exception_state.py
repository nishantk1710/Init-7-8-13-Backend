"""i8_exception_state

FR-11: the exception queue becomes something people work, not only something
they look at. Two tables, and neither one stores an exception.

The items stay computed -- recomputed from the register and the attestation
table on every read, so the queue cannot drift away from the data. What a
check cannot derive is who owns a finding and what was said about it, and that
is what lands here.

``i8_exception_state`` is keyed by the exception's own deterministic id
(``EX-MISSING_ATTESTATION-4500000677-20``) rather than a surrogate, so the
state re-attaches whenever the check raises the same finding again. It is NOT
a foreign key: there is no table of exceptions to point at, by design.

``i8_exception_event`` is append-only, like i8_attestation and for the same
reason -- a history that can be tidied up afterwards is not a history. It is
not protected by a trigger the way attestations are, because this records
workflow rather than a person's professional judgement; the audit weight is
different and a trigger per table is not free on Azure SQL.

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revision ID: c7d2e94a1f36
Revises: a1c6f20d9b84
Create Date: 2026-10-12 19:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2e94a1f36'
down_revision: Union[str, Sequence[str], None] = 'a1c6f20d9b84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i8_exception_state',
        # 128 rather than 64: the id carries the check name and the full SAP
        # document and item, and MISSING_ATTESTATION on a long document number
        # is already 38 characters before anything is added to the vocabulary.
        sa.Column('exception_id', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False,
                  server_default='OPEN'),
        # Nullable on purpose: unassigned is a real state, and the default
        # must not be "whoever touched it last".
        sa.Column('owner', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('exception_id'),
    )
    op.create_index(
        'ix_i8_exception_state_status', 'i8_exception_state', ['status']
    )
    op.create_index(
        'ix_i8_exception_state_owner', 'i8_exception_state', ['owner']
    )

    op.create_table(
        'i8_exception_event',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('exception_id', sa.String(length=128), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        # Null on a comment or an assignment: those do not move the item.
        sa.Column('status', sa.String(length=16), nullable=True),
        sa.Column('actor', sa.String(length=128), nullable=False),
        sa.Column('text', sa.Text(), nullable=False, server_default=''),
        sa.Column('at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_i8_exception_event_exception_id', 'i8_exception_event',
        ['exception_id']
    )
    op.create_index('ix_i8_exception_event_kind', 'i8_exception_event', ['kind'])
    op.create_index('ix_i8_exception_event_at', 'i8_exception_event', ['at'])
    # The drawer's access pattern: one exception's history, oldest first.
    op.create_index(
        'ix_i8_exception_event_exception_at', 'i8_exception_event',
        ['exception_id', 'at']
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the workflow, not the findings: every item comes back on the next
    read because the checks recompute it. What is lost is who owned what and
    what they said, which is exactly the thing this revision added.
    """
    op.drop_index('ix_i8_exception_event_exception_at', table_name='i8_exception_event')
    op.drop_index('ix_i8_exception_event_at', table_name='i8_exception_event')
    op.drop_index('ix_i8_exception_event_kind', table_name='i8_exception_event')
    op.drop_index('ix_i8_exception_event_exception_id', table_name='i8_exception_event')
    op.drop_table('i8_exception_event')

    op.drop_index('ix_i8_exception_state_owner', table_name='i8_exception_state')
    op.drop_index('ix_i8_exception_state_status', table_name='i8_exception_state')
    op.drop_table('i8_exception_state')
