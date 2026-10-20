"""i7_staged_material_plant.mrp_controller

Adds MARC-DISPO (the MRP controller) to I07's staged material-plant table. The
OAR rule now requires DISPO = OAR, as I13 already does (VZI ruling of
2026-10-20), so I07 has to stage the field.

One nullable column and nothing else. Existing rows read NULL until the next I07
staging run fills it -- and NULL is "not OAR" under the new rule, so until that
run the feature build would classify every material-plant OUT_OF_SCOPE. Run the
staging load before the feature build.

NOT APPLIED ANYWHERE by this change. Apply it on purpose (``alembic upgrade
head`` from the SSH console, or RUN_MIGRATIONS_ON_STARTUP=true) after the
deploy and before the next pipeline run: the ORM model already names the
column, so reading i7_staged_material_plant fails until it exists. Rollback:
``alembic downgrade -1`` drops the column.

Portable constructs only -- this applies identically on Postgres and Azure SQL.

Revision ID: d4e8b2a61c35
Revises: c7d3a1e9f402
Create Date: 2026-10-20 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e8b2a61c35'
down_revision: Union[str, Sequence[str], None] = 'c7d3a1e9f402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i7_staged_material_plant',
        sa.Column('mrp_controller', sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_staged_material_plant', 'mrp_controller')
