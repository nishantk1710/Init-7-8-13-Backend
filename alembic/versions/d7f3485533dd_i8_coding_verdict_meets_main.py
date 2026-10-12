"""i8 coding verdict meets main

A merge revision, and nothing else: it creates no table and alters no
column. Two migration lines existed in parallel -- i8_coding_verdict on the
I08 branch, and main's own chain -- and `alembic upgrade head` refuses to run
with two heads. This joins them so there is one again.

Both upgrade() and downgrade() are empty on purpose. Everything either line
does has already been done by the revision it points at; a merge revision that
touched the schema would be doing work neither branch had reviewed.

Revision ID: d7f3485533dd
Revises: a1c6f20d9b84, c7a3f9e12d64
Create Date: 2026-10-12 23:05:06.283379

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f3485533dd'
down_revision: Union[str, Sequence[str], None] = ('a1c6f20d9b84', 'c7a3f9e12d64')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
