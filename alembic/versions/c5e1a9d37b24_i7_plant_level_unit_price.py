"""i7_staged_material_plant: the unit price, per plant

Revision ID: c5e1a9d37b24
Revises: b4d8e2f61a07
Create Date: 2026-10-22

I07 staged ONE unit price per material -- the highest MBEW price across valuation
areas -- and every plant of that material used it. For sodium cyanide that was
7,689.26, taken from two dormant plants, at plants where the material is valued at
14.58 and 15.45: a 500x error in every working-capital figure.

A price now belongs to a plant. Four columns on ``i7_staged_material_plant``:

* ``unit_price`` / ``currency`` -- this plant's own replacement price and the
  currency SAP stated for it;
* ``price_valuation_type`` -- the BWTAR it was read from (NEW / NEW-INS, or none);
* ``price_missing_reason`` -- why there is no price, as a code, indexed so the
  affected material-plants can be listed and counted.

All nullable and none backfilled: a price cannot be reconstructed from the old
per-material one (that is the defect), so existing rows read NULL -- an honest
"not staged" -- until the next staging run fills them. ``i7_staged_material``'s
own ``unit_price`` / ``currency`` are left in place, retired and written NULL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e1a9d37b24"
down_revision: Union[str, Sequence[str], None] = "b4d8e2f61a07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("i7_staged_material_plant", sa.Column("unit_price", sa.Numeric(18, 4), nullable=True))
    op.add_column("i7_staged_material_plant", sa.Column("currency", sa.String(length=8), nullable=True))
    op.add_column(
        "i7_staged_material_plant", sa.Column("price_valuation_type", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "i7_staged_material_plant", sa.Column("price_missing_reason", sa.String(length=32), nullable=True)
    )
    op.create_index(
        op.f("ix_i7_staged_material_plant_price_missing_reason"),
        "i7_staged_material_plant",
        ["price_missing_reason"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_i7_staged_material_plant_price_missing_reason"), table_name="i7_staged_material_plant"
    )
    op.drop_column("i7_staged_material_plant", "price_missing_reason")
    op.drop_column("i7_staged_material_plant", "price_valuation_type")
    op.drop_column("i7_staged_material_plant", "currency")
    op.drop_column("i7_staged_material_plant", "unit_price")
