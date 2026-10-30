"""i7 consumption window: the window a feature run read, and each row's windowed gate

Revision ID: e6a3c91d5b28
Revises: c5e1a9d37f02
Create Date: 2026-10-29 10:00:00.000000

``i7_feature_run`` records the demand window the build read (status, first and last month, and the months inside
it or after it that are missing or incomplete); ``i7_material_feature.window_history_status`` is the history gate
over that window. All nullable: a run or row built before this migration has no window, and NULL says so.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6a3c91d5b28'
down_revision: Union[str, Sequence[str], None] = 'c5e1a9d37f02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('i7_feature_run', sa.Column('window_status', sa.String(length=32), nullable=True))
    op.add_column('i7_feature_run', sa.Column('window_start', sa.Date(), nullable=True))
    op.add_column('i7_feature_run', sa.Column('window_end', sa.Date(), nullable=True))
    op.add_column('i7_feature_run', sa.Column('window_missing_months', sa.String(length=255), nullable=True))
    op.add_column('i7_feature_run', sa.Column('window_trailing_months', sa.String(length=255), nullable=True))
    op.add_column('i7_material_feature', sa.Column('window_history_status', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_material_feature', 'window_history_status')
    op.drop_column('i7_feature_run', 'window_trailing_months')
    op.drop_column('i7_feature_run', 'window_missing_months')
    op.drop_column('i7_feature_run', 'window_end')
    op.drop_column('i7_feature_run', 'window_start')
    op.drop_column('i7_feature_run', 'window_status')
