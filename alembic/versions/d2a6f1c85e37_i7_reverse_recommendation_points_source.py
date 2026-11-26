"""i7 Min-Max to OAR: record who wrote each stored reason (a model or the standard wording)

Revision ID: d2a6f1c85e37
Revises: c7e41d9a2b58
Create Date: 2026-11-26 16:00:00.000000

One nullable column on the Min-Max to OAR recommendation table. A row stored before it existed reads as the standard wording.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2a6f1c85e37"
down_revision: Union[str, Sequence[str], None] = "c7e41d9a2b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("i7_reverse_recommendation", sa.Column("points_source", sa.String(length=24), nullable=True))


def downgrade() -> None:
    op.drop_column("i7_reverse_recommendation", "points_source")
