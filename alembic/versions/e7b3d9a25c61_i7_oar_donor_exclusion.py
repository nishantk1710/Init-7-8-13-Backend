"""i7_oar_donor_exclusion: a user's "do not use this similar material for this target"

Revision ID: e7b3d9a25c61
Revises: c5e1a9d37b24
Create Date: 2026-10-25

One new table, nothing else touched. A row says donor D must not be used when sizing target T. It is
keyed by the target material-plant rather than by OAR run, so it outlives every run and is applied by
every later one (``oar.service.finalise_target``).

A restore keeps the row (``restored_at`` / ``restored_by`` set) and moves ``active_slot`` from 0 to the
row's own id. That is what lets one portable unique constraint say "at most one ACTIVE exclusion per
target and donor" while any number of restored rows stay as history.

No foreign keys, on purpose: ``i7_oar_neighbour`` / ``i7_oar_target`` rows belong to one run and the
exclusion must not.

The downgrade drops only this table, and with it the audit history of exclusions.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b3d9a25c61"
down_revision: Union[str, Sequence[str], None] = "c5e1a9d37b24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "i7_oar_donor_exclusion",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sap_material_number", sa.String(length=40), nullable=False),
        sa.Column("sap_plant_code", sa.String(length=8), nullable=False),
        sa.Column("donor_material", sa.String(length=40), nullable=False),
        sa.Column("donor_plant", sa.String(length=8), nullable=False),
        sa.Column("donor_similarity", sa.Numeric(9, 6), nullable=True),
        sa.Column("donor_rank", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("restored_by", sa.String(length=64), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_slot", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sap_material_number",
            "sap_plant_code",
            "donor_material",
            "donor_plant",
            "active_slot",
            name="uq_i7_oar_donor_exclusion_active",
        ),
    )
    op.create_index(
        "ix_i7_oar_donor_exclusion_target",
        "i7_oar_donor_exclusion",
        ["sap_material_number", "sap_plant_code"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_i7_oar_donor_exclusion_target", table_name="i7_oar_donor_exclusion")
    op.drop_table("i7_oar_donor_exclusion")
