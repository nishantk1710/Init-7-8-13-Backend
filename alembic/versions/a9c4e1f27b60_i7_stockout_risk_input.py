"""i7_stockout_risk_input

What the High Stockout Risk card reads about each material-plant's usage: its
last goods-issue date and its net issued quantity over the 90 days ending on the
configured anchor date (I7_STOCKOUT_RISK_AS_OF).

A new table and nothing else -- no existing table or column is touched. Until
``python -m app.initiatives.i7.stockout_risk`` has filled it, the card reads
"Not available" rather than a number, so applying this ahead of the build is
harmless and so is rolling it back.

NOT APPLIED ANYWHERE by this change. Apply it on purpose (``alembic upgrade
head`` from the SSH console, or RUN_MIGRATIONS_ON_STARTUP=true) after reviewing
it. Rollback: ``alembic downgrade -1`` drops the table and its indexes; nothing
else references it.

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revision ID: a9c4e1f27b60
Revises: b4e7d2a91c35
Create Date: 2026-10-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c4e1f27b60'
down_revision: Union[str, Sequence[str], None] = 'b4e7d2a91c35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i7_stockout_risk_input',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('sap_material_number', sa.String(length=40), nullable=False),
        sa.Column('sap_plant_code', sa.String(length=8), nullable=False),
        sa.Column('as_of_date', sa.Date(), nullable=False),
        sa.Column('last_issue_date', sa.Date(), nullable=False),
        sa.Column('consumption_90d', sa.Numeric(precision=18, scale=3), nullable=False),
        sa.Column('window_days', sa.Integer(), nullable=False),
        sa.Column('built_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'sap_material_number', 'sap_plant_code', name='uq_i7_stockout_risk_input_key'
        ),
    )
    op.create_index(
        'ix_i7_stockout_risk_input_sap_material_number',
        'i7_stockout_risk_input',
        ['sap_material_number'],
        unique=False,
    )
    op.create_index(
        'ix_i7_stockout_risk_input_sap_plant_code',
        'i7_stockout_risk_input',
        ['sap_plant_code'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_i7_stockout_risk_input_sap_plant_code', table_name='i7_stockout_risk_input')
    op.drop_index('ix_i7_stockout_risk_input_sap_material_number', table_name='i7_stockout_risk_input')
    op.drop_table('i7_stockout_risk_input')
