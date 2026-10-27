"""i8_attestation typed name, amendment reason and repair document type

Three gaps in the declaration record, found in the 23-Oct-2026 review:

* ``attestor`` is a server-set placeholder until Entra sign-in is wired, so a
  declaration carried no name at all. ``attestor_name`` holds the name the
  person typed -- labelled as typed, not as a sign-in.
* An amendment could replace a declaration with no reason given. The record is
  append-only so nothing is lost, but a correction with no "why" reads as a
  quiet rewrite. ``amendment_reason`` is required for amendments from now on.
* A declaration could only name a repair PO line. Most repairs sit as an open
  repair PR for weeks before a PO exists; ``repair_document_type`` ('PO' or
  'PR') says which kind of document ``repair_document`` / ``repair_item`` name.
  Null on every existing row: they are all PO lines or unbound.

Additive only: three nullable columns. Nothing is rewritten, so the
append-only guarantee and the UPDATE trigger on this table are untouched.
Portable constructs only -- this applies identically on Postgres and Azure SQL.

Revises ``d2f6b8a41c93`` (I07 KPI formula versions), dev's head on 27-Oct-2026.
Written against main's ``e7b3d9a25c61`` and re-pointed when the branch was
rebased onto dev, so the chain has one head.

Revision ID: f3a9c1e57d20
Revises: d2f6b8a41c93
Create Date: 2026-10-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1e57d20'
down_revision: Union[str, Sequence[str], None] = 'd2f6b8a41c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i8_attestation',
        sa.Column('repair_document_type', sa.String(length=2), nullable=True),
    )
    op.add_column(
        'i8_attestation',
        sa.Column('attestor_name', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'i8_attestation',
        sa.Column('amendment_reason', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema. Drops the three columns and what they recorded."""
    op.drop_column('i8_attestation', 'amendment_reason')
    op.drop_column('i8_attestation', 'attestor_name')
    op.drop_column('i8_attestation', 'repair_document_type')
