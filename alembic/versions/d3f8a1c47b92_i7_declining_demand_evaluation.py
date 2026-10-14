"""i7_declining_demand_evaluation

The declining-demand evaluation: one row per material-plant per feature run,
written by the feature build when I7_DECLINING_DEMAND_MODE is shadow or active.

A new table and nothing else -- no existing table or column is touched, and the
code reads it only when the mode is not "off", so applying this ahead of the
feature is harmless and so is rolling it back.

NOT APPLIED ANYWHERE by this change. Apply it on purpose (``alembic upgrade
head`` from the SSH console, or RUN_MIGRATIONS_ON_STARTUP=true) after reviewing
it. Rollback: ``alembic downgrade -1`` drops the table and its indexes; nothing
else references it.

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revision ID: d3f8a1c47b92
Revises: e5b8a3c61d20
Create Date: 2026-10-14 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3f8a1c47b92'
down_revision: Union[str, Sequence[str], None] = 'e5b8a3c61d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i7_declining_demand_evaluation',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('feature_run_id', sa.Integer(), nullable=False),
        sa.Column('sap_material_number', sa.String(length=40), nullable=False),
        sa.Column('sap_plant_code', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('reason', sa.String(length=255), nullable=False),
        sa.Column('rule_version', sa.String(length=32), nullable=False),
        sa.Column('window_start', sa.Date(), nullable=True),
        sa.Column('window_end', sa.Date(), nullable=True),
        sa.Column('months_evaluated', sa.Integer(), nullable=False),
        sa.Column('nonzero_months', sa.Integer(), nullable=True),
        sa.Column('early_nonzero', sa.Integer(), nullable=True),
        sa.Column('recent_nonzero', sa.Integer(), nullable=True),
        sa.Column('early_rate', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('recent_rate', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('decline_pct', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('latest_months_zero', sa.Boolean(), nullable=True),
        sa.Column('evaluated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'feature_run_id', 'sap_material_number', 'sap_plant_code', name='uq_i7_declining_eval_key'
        ),
    )
    op.create_index(
        'ix_i7_declining_eval_run_status',
        'i7_declining_demand_evaluation',
        ['feature_run_id', 'status'],
        unique=False,
    )
    op.create_index(
        'ix_i7_declining_eval_material_plant',
        'i7_declining_demand_evaluation',
        ['sap_material_number', 'sap_plant_code'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_i7_declining_eval_material_plant', table_name='i7_declining_demand_evaluation')
    op.drop_index('ix_i7_declining_eval_run_status', table_name='i7_declining_demand_evaluation')
    op.drop_table('i7_declining_demand_evaluation')
