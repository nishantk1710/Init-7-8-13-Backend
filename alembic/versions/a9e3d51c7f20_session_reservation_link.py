"""session_reservation_link and uat_reservation_sgtxt -- reading the session ID back off SGTXT

Revision ID: a9e3d51c7f20
Revises: f2a7c19d4b83
Create Date: 2026-09-25

The requester types the assistant's session ID into the reservation's item text
(RESB.SGTXT, raw_resb.text) -- the carrier agreed with the SAP team while BEDNR
is not in the extract (blocker B2). ``session_reservation_link`` holds what the
linker reads back; ``uat_reservation_sgtxt`` lets UAT stand in for SAP. See
``app/models/i13_session_link.py`` for why neither is append-only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9e3d51c7f20"
down_revision: Union[str, Sequence[str], None] = "f2a7c19d4b83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_reservation_link",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("reservation_number", sa.String(length=20), nullable=False),
        sa.Column("reservation_item", sa.String(length=10), nullable=False),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("sgtxt", sa.String(length=60), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_reservation_link")),
        sa.UniqueConstraint("session_id", "reservation_number", "reservation_item", name="uq_session_reservation_link"),
    )
    op.create_index(op.f("ix_session_reservation_link_session_id"), "session_reservation_link", ["session_id"])
    op.create_index(
        op.f("ix_session_reservation_link_reservation_number"), "session_reservation_link", ["reservation_number"]
    )
    op.create_index(op.f("ix_session_reservation_link_material"), "session_reservation_link", ["material"])
    op.create_index(op.f("ix_session_reservation_link_plant"), "session_reservation_link", ["plant"])

    op.create_table(
        "uat_reservation_sgtxt",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reservation_number", sa.String(length=20), nullable=False),
        sa.Column("reservation_item", sa.String(length=10), nullable=False),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=10), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("requirement_date", sa.Date(), nullable=True),
        sa.Column("requirement_quantity", sa.Numeric(18, 3), nullable=True),
        sa.Column("sgtxt", sa.String(length=60), nullable=False),
        sa.Column("original_sgtxt", sa.String(length=60), nullable=True),
        sa.Column("session_id", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uat_reservation_sgtxt")),
        sa.UniqueConstraint("reservation_number", "reservation_item", name="uq_uat_reservation_sgtxt"),
    )
    op.create_index(op.f("ix_uat_reservation_sgtxt_reservation_number"), "uat_reservation_sgtxt", ["reservation_number"])
    op.create_index(op.f("ix_uat_reservation_sgtxt_material"), "uat_reservation_sgtxt", ["material"])
    op.create_index(op.f("ix_uat_reservation_sgtxt_session_id"), "uat_reservation_sgtxt", ["session_id"])


def downgrade() -> None:
    op.drop_table("uat_reservation_sgtxt")
    op.drop_table("session_reservation_link")
