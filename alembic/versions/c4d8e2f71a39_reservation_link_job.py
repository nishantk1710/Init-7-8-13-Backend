"""reservation link job

One row per press of "Link reservation" on a request: which step it is on, what
each step did, and a heartbeat, so every worker answers the progress window the
same way (``app.assistant.link_job``).

Portable constructs only. No trigger: the runner updates the row as it moves on.

Revision ID: c4d8e2f71a39
Revises: b7e3a91d5c28
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4d8e2f71a39"
down_revision: Union[str, Sequence[str], None] = "b7e3a91d5c28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reservation_link_job",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("steps", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delta_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delta_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("links", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservation_link_job")),
    )
    op.create_index(op.f("ix_reservation_link_job_request_id"), "reservation_link_job", ["request_id"], unique=False)
    op.create_index(op.f("ix_reservation_link_job_status"), "reservation_link_job", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_reservation_link_job_status"), table_name="reservation_link_job")
    op.drop_index(op.f("ix_reservation_link_job_request_id"), table_name="reservation_link_job")
    op.drop_table("reservation_link_job")
