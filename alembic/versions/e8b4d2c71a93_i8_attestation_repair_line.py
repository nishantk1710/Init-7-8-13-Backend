"""i8_attestation repair line binding

FR-4 asks for an attestation to be linked "once raised, to the repair PR or
PO". It never was. Coverage was decided entirely by a fuzzy match -- same
material, same plant, and the declaration's timestamp within a configured
window of the repair line's raised date (``attestation_window_days``, 30).

That rule was an engineering proposal, not a VZI ruling, and on the deployed
data it matches nothing: open repair lines were raised between 2018 and 2026
and only one open line in 500 is less than thirty days old, so a declaration
stamped today sits years from every line it could cover. Measured 13-Oct-2026:
two attestations recorded, 5,155 of 5,155 lines still reading Required.

These two columns carry the key the register is actually built on, so a
declaration made FROM a repair line -- which is most of the screens -- binds
exactly and needs no window at all.

**Nullable on purpose, and they must stay nullable.** The control exists for
the part coming off a machine, assessed before any repair is raised; in that
moment there is no purchase order to point at. Those declarations are stored
unbound, and the window remains their fallback. The window is not deleted
here, it is demoted to the one case where it can be defended.

Additive only: two nullable columns and an index. Nothing is rewritten, so the
append-only guarantee and the UPDATE trigger on this table are untouched, and
every existing row keeps matching exactly as it did.

Portable constructs only -- this applies identically on Postgres and Azure SQL.

Revision ID: e8b4d2c71a93
Revises: c7d2e94a1f36
Create Date: 2026-10-13 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8b4d2c71a93'
down_revision: Union[str, Sequence[str], None] = 'c7d2e94a1f36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # EKPO.EBELN. 20 rather than 10: the register's own document column is
    # sized for what the extract carries, and an attestation that cannot store
    # the document it names would fail at write time rather than at review.
    op.add_column(
        'i8_attestation',
        sa.Column('repair_document', sa.String(length=20), nullable=True),
    )
    # EKPO.EBELP.
    op.add_column(
        'i8_attestation',
        sa.Column('repair_item', sa.String(length=10), nullable=True),
    )
    # "What was declared against THIS repair line?", once per refresh. The
    # material/plant/attested_at index stays: unbound declarations still match
    # through the window and still need it.
    op.create_index(
        'ix_i8_attestation_repair_line',
        'i8_attestation',
        ['repair_document', 'repair_item'],
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the binding. Any declaration that was covering a line only through
    this link goes back to being judged by the window, which on this data means
    it covers nothing -- the rows survive, their coverage does not.
    """
    op.drop_index('ix_i8_attestation_repair_line', table_name='i8_attestation')
    op.drop_column('i8_attestation', 'repair_item')
    op.drop_column('i8_attestation', 'repair_document')
