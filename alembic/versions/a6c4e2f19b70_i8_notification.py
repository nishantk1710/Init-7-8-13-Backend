"""i8_notification

FR-11 asks for exception notifications "through the exception queue and email".
This is the log of what should be sent: one row per exception id, written the
first time the check finds that item actionable (not raised before its control
existed), for the types named in I8_NOTIFICATION_TYPES. Nothing is emailed yet;
``emailed_at`` is reserved for the sender that comes later and is null on every
row until then.

The exception itself is still not stored -- items are recomputed on every read.
``exception_id`` is unique so a second detection of the same finding cannot
become a second notification, and the writer relies on that.

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revises ``f3a9c1e57d20``, this branch's head on 27-Oct-2026. origin/dev adds
``d2f6b8a41c93`` on top of main's head, so when this branch is merged into dev
the chain must be re-pointed there (see ``f3a9c1e57d20``) and this revision
stays after it.

Revision ID: a6c4e2f19b70
Revises: f3a9c1e57d20
Create Date: 2026-10-27 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6c4e2f19b70'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1e57d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i8_notification',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('exception_id', sa.String(length=128), nullable=False),
        sa.Column('exception_type', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('material_id', sa.String(length=40), nullable=False),
        sa.Column('plant', sa.String(length=8), nullable=True),
        sa.Column('channel', sa.String(length=16), nullable=False,
                  server_default='EMAIL'),
        sa.Column('logged_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('emailed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_i8_notification_exception_id', 'i8_notification',
        ['exception_id'], unique=True,
    )
    op.create_index(
        'ix_i8_notification_exception_type', 'i8_notification',
        ['exception_type'],
    )


def downgrade() -> None:
    """Downgrade schema. Drops the log; the exceptions come back on the next
    read, but which ones were already notified is lost."""
    op.drop_index('ix_i8_notification_exception_type', table_name='i8_notification')
    op.drop_index('ix_i8_notification_exception_id', table_name='i8_notification')
    op.drop_table('i8_notification')
