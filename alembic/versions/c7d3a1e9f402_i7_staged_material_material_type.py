"""i7_staged_material.material_type

Adds MTART (material type: ERSA spare part, HIBE consumable, ...) to I07's staged
material master. The OAR similarity engine uses it as a hard constraint: a
neighbour must share the target's material type when both are known
(oar-similarity-5).

One nullable column and nothing else. Existing rows read NULL until the next
I07 staging run fills it -- NULL is "blank", which the constraint skips, so the
OAR engine behaves exactly as before on rows staged before this.

NOT APPLIED ANYWHERE by this change. Apply it on purpose (``alembic upgrade
head`` from the SSH console, or RUN_MIGRATIONS_ON_STARTUP=true) after the
deploy and before the next pipeline run: the ORM model already names the
column, so reading i7_staged_material fails until it exists. Rollback:
``alembic downgrade -1`` drops the column.

Portable constructs only -- this applies identically on Postgres and Azure SQL.

Revision ID: c7d3a1e9f402
Revises: a9c4e1f27b60
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d3a1e9f402'
down_revision: Union[str, Sequence[str], None] = 'a9c4e1f27b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i7_staged_material',
        sa.Column('material_type', sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_staged_material', 'material_type')
