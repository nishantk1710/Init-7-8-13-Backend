"""i7_sap_adoption: the day SAP made the change (adopted_on)

One nullable ``Date`` column on ``i7_sap_adoption``: the day of the SAP change that confirmed an adoption, read from SAP's change
history (CDHDR's change date, joined to CDPOS for the field). It is what lets the report place a confirmed adoption in a month;
``evaluated_at`` cannot, because it is when the check last ran.

Nothing is back-filled. An adoption recorded before this change keeps NULL ("date not known") and is not placed in any month; the
next daily adoption check fills it in for a result that is still ADOPTED when it is re-evaluated. Existing rows are not touched.

NOT APPLIED ANYWHERE by this change. Apply it on purpose (``alembic upgrade head`` from the SSH console, or
RUN_MIGRATIONS_ON_STARTUP=true) after the deploy. Rollback: ``alembic downgrade -1`` drops the column.

Portable constructs only -- this applies identically on Postgres and Azure SQL.

Revision ID: a7c3e9d15b82
Revises: e5b7c1d93a64
Create Date: 2026-11-06 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d15b82'
down_revision: Union[str, Sequence[str], None] = 'e5b7c1d93a64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('i7_sap_adoption', sa.Column('adopted_on', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_sap_adoption', 'adopted_on')
