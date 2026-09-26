"""merge i7, assistant and csv_extract heads

Revision ID: af8f9acb958a
Revises: 0bffdfda3487, a9e3d51c7f20, c5f1a8b90d34
Create Date: 2026-09-26 13:00:19.837764

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'af8f9acb958a'
down_revision: Union[str, Sequence[str], None] = ('0bffdfda3487', 'a9e3d51c7f20', 'c5f1a8b90d34')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
