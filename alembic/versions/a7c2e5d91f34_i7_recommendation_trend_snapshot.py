"""i7_recommendation_trend_snapshot

The history behind the Overview's two trend cards (Stockout risk trend, Excess inventory
opportunity): one row per recommendation-generation run per plant scope (ALL / 1300 / 1500),
written once at the end of a successful generation and never changed.

A new table and nothing else -- no existing table or column is touched. The application reads
and writes it only through the recommendation generator's recorder and the trends endpoint, so
applying this ahead of the code is harmless, and so is rolling it back.

NOT APPLIED ANYWHERE by this change. Review it, then apply on purpose (``alembic upgrade head``
from the SSH console, or RUN_MIGRATIONS_ON_STARTUP=true). It creates an empty table; no data is
written until the next successful recommendation generation, or until an operator runs the
excess-history backfill (``python -m app.initiatives.i7.recommendations.trend_backfill``, which
prints what it would write before ``--apply``).

Rollback: ``alembic downgrade -1`` drops the table and its index. Nothing references it, and
nothing else in the schema changes, so the only thing lost is the history recorded since.

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revision ID: a7c2e5d91f34
Revises: d3f8a1c47b92
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c2e5d91f34'
down_revision: Union[str, Sequence[str], None] = 'd3f8a1c47b92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i7_recommendation_trend_snapshot',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('generation_key', sa.String(length=160), nullable=False),
        sa.Column('plant_scope', sa.String(length=8), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('inventory_run_id', sa.Integer(), nullable=True),
        sa.Column('oar_run_id', sa.Integer(), nullable=True),
        sa.Column('forecast_run_id', sa.Integer(), nullable=True),
        sa.Column('feature_run_id', sa.Integer(), nullable=True),
        sa.Column('formula_version', sa.String(length=64), nullable=True),
        sa.Column('total_count', sa.Integer(), nullable=False),
        sa.Column('stockout_risk_count', sa.Integer(), nullable=True),
        sa.Column('excess_count', sa.Integer(), nullable=False),
        sa.Column('excess_opportunity', sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column('net_safety_stock_value_impact', sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column('currency', sa.String(length=8), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('generation_key', 'plant_scope', name='uq_i7_trend_snapshot_generation_scope'),
    )
    op.create_index(
        'ix_i7_trend_snapshot_scope_time',
        'i7_recommendation_trend_snapshot',
        ['plant_scope', 'captured_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_i7_trend_snapshot_scope_time', table_name='i7_recommendation_trend_snapshot')
    op.drop_table('i7_recommendation_trend_snapshot')
