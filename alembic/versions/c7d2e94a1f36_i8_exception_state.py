"""i8_exception_event

FR-11: the exception queue becomes something people work, not only something
they look at. One table, and it does not store an exception.

The items stay computed -- recomputed from the register and the attestation
table on every read, so the queue cannot drift away from the data. What a
check cannot derive is who owns a finding and what was said about it, and that
is what lands here.

There is no companion "state" table holding status and owner. Those are folded
from this log: the last entry that set a status, the owner the last ASSIGNED
entry named. A state table would have to be UPDATEd, and every table this
application owns is append-only -- a rule tests/test_write_paths.py holds the
whole app to. Folding also means the current state and the audit trail cannot
disagree, because they are the same rows read two ways.

Keyed by the exception's own deterministic id
(``EX-MISSING_ATTESTATION-4500000677-20``) rather than a surrogate, so the
history re-attaches whenever the check raises the same finding again. It is NOT
a foreign key: there is no table of exceptions to point at, by design.

No immutability trigger, unlike i8_attestation. That table records a person's
professional judgement and is worth a trigger per row; this records workflow,
the API exposes no verb that could edit it, and a trigger is not free on Azure
SQL.

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revision ID: c7d2e94a1f36
Revises: d7f3485533dd
Create Date: 2026-10-12 19:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2e94a1f36'
# Re-pointed onto the merge revision when main was brought into this branch.
# This creates a new table and depends on nothing either line changed, so it
# simply sits after the join rather than forking a second head.
down_revision: Union[str, Sequence[str], None] = 'd7f3485533dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i8_exception_event',
        sa.Column('id', sa.String(length=32), nullable=False),
        # 128 rather than 64: the id carries the check name and the full SAP
        # document and item, and MISSING_ATTESTATION on a long document number
        # is already 38 characters before anything is added to the vocabulary.
        sa.Column('exception_id', sa.String(length=128), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        # Null on a comment or an assignment: those do not move the item.
        sa.Column('status', sa.String(length=16), nullable=True),
        # Set on an ASSIGNED entry. Null there means UNASSIGNED -- handing
        # something back is an action, and `kind` tells the two nulls apart.
        sa.Column('owner', sa.String(length=128), nullable=True),
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
    op.create_index('ix_i8_exception_event_owner', 'i8_exception_event', ['owner'])
    op.create_index('ix_i8_exception_event_at', 'i8_exception_event', ['at'])
    # The drawer's access pattern, and the fold's: one exception's history,
    # oldest first.
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
    op.drop_index('ix_i8_exception_event_owner', table_name='i8_exception_event')
    op.drop_index('ix_i8_exception_event_kind', table_name='i8_exception_event')
    op.drop_index('ix_i8_exception_event_exception_id', table_name='i8_exception_event')
    op.drop_table('i8_exception_event')
