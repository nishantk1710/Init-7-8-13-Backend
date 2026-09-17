"""i13_watch_metric_mart

Revision ID: ba29ede70827
Revises: 770e63ee2f68
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ba29ede70827'
down_revision: Union[str, Sequence[str], None] = '770e63ee2f68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i13_watch_metric_mart',
        sa.Column('material', sa.String(length=40), nullable=False),
        sa.Column('plant', sa.String(length=10), nullable=False),
        sa.Column('material_scope', sa.String(length=16), nullable=False),
        sa.Column('stock_on_hand', sa.Numeric(18, 6), nullable=True),
        sa.Column('open_po_quantity', sa.Numeric(18, 6), nullable=False),
        sa.Column('average_monthly_consumption', sa.Numeric(18, 6), nullable=False),
        sa.Column('months_of_cover', sa.Numeric(18, 6), nullable=True),
        sa.Column('projected_months_of_cover', sa.Numeric(18, 6), nullable=True),
        sa.Column('months_of_cover_reason', sa.String(length=40), nullable=True),
        sa.Column('last_movement_date', sa.Date(), nullable=True),
        sa.Column('days_since_last_movement', sa.Integer(), nullable=True),
        sa.Column('last_issue_date', sa.Date(), nullable=True),
        sa.Column('days_since_last_issue', sa.Integer(), nullable=True),
        sa.Column('consumption_count_12m', sa.Integer(), nullable=False),
        sa.Column('consumed_qty_12m', sa.Numeric(18, 6), nullable=False),
        sa.Column('inventory_turns', sa.Numeric(18, 6), nullable=True),
        sa.Column('inventory_turns_reason', sa.String(length=40), nullable=True),
        sa.Column('aging_band', sa.String(length=16), nullable=False),
        sa.Column('gr_not_issued_flag', sa.Boolean(), nullable=False),
        sa.Column('gr_not_issued_days_since_gr', sa.Integer(), nullable=True),
        sa.Column('gr_not_issued_relevant_gr_date', sa.Date(), nullable=True),
        sa.Column('gr_not_issued_threshold_days', sa.Integer(), nullable=False),
        sa.Column('gr_not_issued_received_quantity', sa.Numeric(18, 6), nullable=False),
        sa.Column('gr_not_issued_issued_quantity', sa.Numeric(18, 6), nullable=False),
        sa.Column('gr_not_issued_outstanding_quantity', sa.Numeric(18, 6), nullable=False),
        sa.Column('acquired_vs_plan_status', sa.String(length=16), nullable=False),
        sa.Column('planned_quantity', sa.Numeric(18, 6), nullable=True),
        sa.Column('received_quantity', sa.Numeric(18, 6), nullable=False),
        sa.Column('issued_quantity', sa.Numeric(18, 6), nullable=False),
        sa.Column('acquired_vs_plan_variance_quantity', sa.Numeric(18, 6), nullable=True),
        sa.Column('acquired_vs_plan_variance_percentage', sa.Numeric(18, 6), nullable=True),
        sa.Column('calculated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('refreshed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('material', 'plant', name=op.f('pk_i13_watch_metric_mart')),
    )
    op.create_index(
        op.f('ix_i13_watch_metric_mart_material_scope'), 'i13_watch_metric_mart', ['material_scope'], unique=False
    )
    op.create_index(op.f('ix_i13_watch_metric_mart_aging_band'), 'i13_watch_metric_mart', ['aging_band'], unique=False)
    op.create_index(
        op.f('ix_i13_watch_metric_mart_gr_not_issued_flag'), 'i13_watch_metric_mart', ['gr_not_issued_flag'], unique=False
    )
    op.create_index(
        op.f('ix_i13_watch_metric_mart_acquired_vs_plan_status'),
        'i13_watch_metric_mart',
        ['acquired_vs_plan_status'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_i13_watch_metric_mart_acquired_vs_plan_status'), table_name='i13_watch_metric_mart')
    op.drop_index(op.f('ix_i13_watch_metric_mart_gr_not_issued_flag'), table_name='i13_watch_metric_mart')
    op.drop_index(op.f('ix_i13_watch_metric_mart_aging_band'), table_name='i13_watch_metric_mart')
    op.drop_index(op.f('ix_i13_watch_metric_mart_material_scope'), table_name='i13_watch_metric_mart')
    op.drop_table('i13_watch_metric_mart')
