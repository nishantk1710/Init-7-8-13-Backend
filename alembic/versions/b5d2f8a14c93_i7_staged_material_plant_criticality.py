"""i7 staging: criticality per material-plant

Revision ID: b5d2f8a14c93
Revises: a7c3e9d15b82
Create Date: 2026-11-06 12:00:00.000000

Criticality is defined per material-plant (the same material can be CRITICAL at one plant and
NORMAL at another), but I07 staged one value per material on ``i7_staged_material`` -- the most
severe across plants. ``i7_staged_material_plant.criticality`` holds each plant's own tier;
``i7_staged_material.criticality`` is retired (staging writes NULL) and kept as a column.

Nullable: NULL means unknown, never NORMAL. Rows staged before this column existed stay NULL
until the next staging run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5d2f8a14c93'
down_revision: Union[str, Sequence[str], None] = 'a7c3e9d15b82'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('i7_staged_material_plant', sa.Column('criticality', sa.String(length=16), nullable=True))
    op.create_index(
        op.f('ix_i7_staged_material_plant_criticality'), 'i7_staged_material_plant', ['criticality'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_i7_staged_material_plant_criticality'), table_name='i7_staged_material_plant')
    op.drop_column('i7_staged_material_plant', 'criticality')
