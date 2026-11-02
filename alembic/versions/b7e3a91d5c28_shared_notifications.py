"""shared notification tables

The notification system shared by every module: the inbox/outbox
(``notification``), who holds which approval role at which plant
(``notification_recipients``), reminder timings (``notification_sla``), the
reminder counters (``notification_reminder_state``) and an append-only audit of
edits to the last two (``notification_config_audit``).

The older ``i8_notification`` and ``i13_act_notification`` tables are not
touched, so their history is kept.

Portable constructs only, plus an MSSQL trigger that makes the audit table
append-only (same pattern as ``e3b7c2d9f104``).

Revision ID: b7e3a91d5c28
Revises: a6d2f81c9e47
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7e3a91d5c28"
down_revision: Union[str, Sequence[str], None] = "a6d2f81c9e47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AUDIT_TRIGGER = """
CREATE TRIGGER notification_config_audit_no_update_or_delete
ON notification_config_audit
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM deleted)
        THROW 50001,
            'notification_config_audit is append-only: it is the history of recipient and SLA changes.',
            1;
END
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "notification",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("module", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=40), nullable=True),
        sa.Column("recipient_email", sa.String(length=320), nullable=True),
        sa.Column("recipient_role", sa.String(length=64), nullable=True),
        sa.Column("plant", sa.String(length=8), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=True),
        sa.Column("entity_id", sa.String(length=128), nullable=True),
        sa.Column("action_path", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification"),
        sa.UniqueConstraint("idempotency_key", name="uq_notification_idempotency_key"),
    )
    op.create_index("ix_notification_recipient_user_id", "notification", ["recipient_user_id"])
    op.create_index("ix_notification_entity_id", "notification", ["entity_id"])
    op.create_index("ix_notification_status", "notification", ["status"])

    op.create_table(
        "notification_recipients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=True),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("user_oid", sa.String(length=36), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("updated_by", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification_recipients"),
    )
    op.create_index("ix_notification_recipients_role", "notification_recipients", ["role"])

    op.create_table(
        "notification_sla",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("approval_route", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=True),
        sa.Column("first_reminder_minutes", sa.Integer(), nullable=False),
        sa.Column("repeat_minutes", sa.Integer(), nullable=False),
        sa.Column("max_reminders", sa.Integer(), nullable=False),
        sa.Column("escalate_after_minutes", sa.Integer(), nullable=True),
        sa.Column("escalation_role", sa.String(length=64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("updated_by", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification_sla"),
        sa.UniqueConstraint("approval_route", "role", name="uq_notification_sla_route_role"),
    )

    op.create_table(
        "notification_reminder_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recommendation_id", sa.String(length=64), nullable=False),
        sa.Column("pending_since_ledger_id", sa.Integer(), nullable=False),
        sa.Column("reminders_sent", sa.Integer(), nullable=False),
        sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification_reminder_state"),
        sa.UniqueConstraint("recommendation_id", "pending_since_ledger_id", name="uq_notification_reminder_stage"),
    )
    op.create_index(
        "ix_notification_reminder_state_recommendation_id", "notification_reminder_state", ["recommendation_id"]
    )

    op.create_table(
        "notification_config_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("table_name", sa.String(length=48), nullable=False),
        sa.Column("row_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=40), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notification_config_audit"),
    )

    if op.get_bind().dialect.name == "mssql":
        op.execute(_AUDIT_TRIGGER)


def downgrade() -> None:
    """Downgrade schema. Drops the shared notification tables and their history."""
    if op.get_bind().dialect.name == "mssql":
        op.execute("DROP TRIGGER IF EXISTS notification_config_audit_no_update_or_delete")
    op.drop_table("notification_config_audit")
    op.drop_index("ix_notification_reminder_state_recommendation_id", table_name="notification_reminder_state")
    op.drop_table("notification_reminder_state")
    op.drop_table("notification_sla")
    op.drop_index("ix_notification_recipients_role", table_name="notification_recipients")
    op.drop_table("notification_recipients")
    op.drop_index("ix_notification_status", table_name="notification")
    op.drop_index("ix_notification_entity_id", table_name="notification")
    op.drop_index("ix_notification_recipient_user_id", table_name="notification")
    op.drop_table("notification")
