"""assistant_session.department_note -- free text beside the department code

Revision ID: b4d8e2f61a07
Revises: a3c9e7f15b42
Create Date: 2026-10-21

The department has been a code picked from a list since 15 Oct. UAT asked for
somewhere to type anything else about it (a section, a crew, who it is on
behalf of). That is this column: optional, typed, never checked -- and kept
apart from ``department`` so the per-department figures still count codes only.

Nullable for the same reason as ``department`` and ``requested_for``:
``assistant_session`` is append-only, so existing rows cannot be backfilled,
and a session opened from SAP has nothing to put here.

Not indexed: nothing filters on it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d8e2f61a07"
down_revision: Union[str, Sequence[str], None] = "a3c9e7f15b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "assistant_session",
        sa.Column("department_note", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("assistant_session", "department_note")
