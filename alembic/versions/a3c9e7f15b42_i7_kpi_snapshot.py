"""i7_kpi_snapshot and i7_kpi_snapshot_log

Saved monthly KPI snapshots for the I07 Quarterly Report: one row per month, so the report's trends and its
quarter-end position come from what was measured at the time and not from a reconstruction. The log table is the
append-only record of every capture, refusal and replacement.

Two new tables and nothing else; no existing table is touched. Nothing reads them until a snapshot is captured
(``python -m app.reporting.capture_snapshot``), and the Quarterly Report treats a missing snapshot as "no snapshot",
never as a zero.

NOT APPLIED ANYWHERE by this change. Apply it on purpose (``alembic upgrade head`` from the SSH console, or
RUN_MIGRATIONS_ON_STARTUP=true) after the deploy and before the first capture. Rollback: ``alembic downgrade -1``
drops both tables (and with them any snapshots taken, which cannot be rebuilt for a past month).

Portable constructs only -- this applies identically on Postgres and Azure SQL.

Revision ID: a3c9e7f15b42
Revises: d4e8b2a61c35
Create Date: 2026-10-21 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c9e7f15b42'
down_revision: Union[str, Sequence[str], None] = 'd4e8b2a61c35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i7_kpi_snapshot',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('month', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('cutoff', sa.Date(), nullable=False),
        sa.Column('measured_on', sa.Date(), nullable=False),
        sa.Column('month_end_valid', sa.Boolean(), nullable=False),
        sa.Column('latest_consumption_date', sa.Date(), nullable=True),
        sa.Column('stockout_usage_as_of', sa.Date(), nullable=True),
        sa.Column('stock_loaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('population', sa.Integer(), nullable=False),
        sa.Column('stockout_at_risk', sa.Integer(), nullable=False),
        sa.Column('stockout_evaluated', sa.Integer(), nullable=False),
        sa.Column('stockout_insufficient', sa.Integer(), nullable=False),
        sa.Column('stockout_stale', sa.Boolean(), nullable=False),
        sa.Column('excess_count', sa.Integer(), nullable=False),
        sa.Column('excess_opportunity', sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column('net_rop_value_impact', sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column('priced_count', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('payload_sha256', sa.String(length=64), nullable=False),
        sa.Column('calc_version', sa.String(length=16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('month', name='uq_i7_kpi_snapshot_month'),
    )
    op.create_index('ix_i7_kpi_snapshot_month', 'i7_kpi_snapshot', ['month'], unique=False)

    op.create_table(
        'i7_kpi_snapshot_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('month', sa.Date(), nullable=False),
        sa.Column('action', sa.String(length=24), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=True),
        sa.Column('previous_sha256', sa.String(length=64), nullable=True),
        sa.Column('payload_sha256', sa.String(length=64), nullable=True),
        sa.Column('detail', sa.String(length=1000), nullable=True),
        sa.Column('logged_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_i7_kpi_snapshot_log_month', 'i7_kpi_snapshot_log', ['month'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_i7_kpi_snapshot_log_month', table_name='i7_kpi_snapshot_log')
    op.drop_table('i7_kpi_snapshot_log')
    op.drop_index('ix_i7_kpi_snapshot_month', table_name='i7_kpi_snapshot')
    op.drop_table('i7_kpi_snapshot')
