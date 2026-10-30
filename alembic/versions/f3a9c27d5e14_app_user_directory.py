"""app_user_directory

The user directory behind Entra sign-in (app/core/user_directory.py): one row
the first time a signed-in user is seen, and another only when their name or
e-mail changes. Lets screens show a name for the Entra object ID recorded as
the author of every write.

A new table and nothing else -- no existing table is touched, so this is safe
to run at start-up on a live database. Portable constructs only.

Written on d2f6b8a41c93, which I08's attestation and notification migrations
(f3a9c1e57d20, a6c4e2f19b70) and its nonprod test-row wipe (c5e1a9d37f02)
followed too; moved after them, and then after I07's consumption-window
migrations (e6a3c91d5b28, f4b8d2a71c63), each time before it had run on any
database, so the history keeps a single head.

Revision ID: f3a9c27d5e14
Revises: f4b8d2a71c63
Create Date: 2026-10-26 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c27d5e14'
down_revision: Union[str, Sequence[str], None] = 'f4b8d2a71c63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'app_user',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('oid', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_app_user')),
    )
    op.create_index(op.f('ix_app_user_oid'), 'app_user', ['oid'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_app_user_oid'), table_name='app_user')
    op.drop_table('app_user')
