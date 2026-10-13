"""i13_summary_report

The I13 summary report: one row per generated report, insert-only. A new
table and nothing else -- no existing table is touched, so this is safe to run
at start-up on a live database (startup.sh runs ``alembic upgrade head``).

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revision ID: e5b8a3c61d20
Revises: e8b4d2c71a93
Create Date: 2026-10-13 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b8a3c61d20'
# Re-pointed after main gained e8b4d2c71a93 (I08 attestation repair line);
# this only creates a new table and depends on nothing that one changed.
down_revision: Union[str, Sequence[str], None] = 'e8b4d2c71a93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i13_summary_report',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('generated_by', sa.String(length=120), nullable=False),
        sa.Column('plant', sa.String(length=10), nullable=True),
        sa.Column('snapshot_version', sa.Integer(), nullable=True),
        sa.Column('data_as_of', sa.Date(), nullable=True),
        sa.Column('headline', sa.String(length=600), nullable=False),
        sa.Column('narrative_source', sa.String(length=16), nullable=False),
        sa.Column('model', sa.String(length=80), nullable=True),
        sa.Column('prompt_id', sa.String(length=64), nullable=True),
        sa.Column('prompt_version', sa.Integer(), nullable=True),
        sa.Column('report_json', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_i13_summary_report_generated_at'), 'i13_summary_report', ['generated_at'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_i13_summary_report_generated_at'), table_name='i13_summary_report')
    op.drop_table('i13_summary_report')
