"""material_plant

Revision ID: b3e77c50a4f2
Revises: 9a1c4d2e7b10
Create Date: 2026-09-21 14:10:00.000000

The first serving table. See app/models/serving.py for why this layer is
migration-managed while odata_* is dropped and rebuilt on every pull.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e77c50a4f2'
down_revision: Union[str, Sequence[str], None] = '9a1c4d2e7b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'material_plant',
        sa.Column('matnr', sa.String(length=18), nullable=False),
        sa.Column('werks', sa.String(length=4), nullable=False),
        sa.Column('mtart', sa.String(length=4), nullable=True),
        sa.Column('matkl', sa.String(length=9), nullable=True),
        sa.Column('meins', sa.String(length=3), nullable=True),
        sa.Column('maktx', sa.String(length=40), nullable=True),
        sa.Column('dismm', sa.String(length=2), nullable=True),
        sa.Column('is_oar', sa.Boolean(), nullable=False),
        sa.Column('eisbe', sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column('minbe', sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column('mabst', sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column('losgr', sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column('plifz', sa.Integer(), nullable=True),
        sa.Column('source_run_date', sa.Date(), nullable=True),
        sa.Column(
            'built_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('matnr', 'werks', name=op.f('pk_material_plant')),
    )
    op.create_index(
        'ix_material_plant_oar', 'material_plant', ['is_oar', 'werks'], unique=False
    )
    op.create_index(
        'ix_material_plant_dismm', 'material_plant', ['dismm'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_material_plant_dismm', table_name='material_plant')
    op.drop_index('ix_material_plant_oar', table_name='material_plant')
    op.drop_table('material_plant')
