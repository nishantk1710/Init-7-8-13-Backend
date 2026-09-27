"""assistant append-only triggers on SQL Server

d8c3b1f04e75 created the immutability trigger on its five tables on Postgres
only, so on Azure SQL a session, turn, justification, consumption plan or
quantity suggestion -- all audit records -- could be edited or deleted
silently. This adds the same guarantee there, exactly as e3b7c2d9f104 did for
i8_attestation: same trigger names, same messages.

Revision ID: 5c9e2a7d4f18
Revises: e3b7c2d9f104
Create Date: 2026-09-27 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "5c9e2a7d4f18"
down_revision: Union[str, Sequence[str], None] = "e3b7c2d9f104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Copied, not imported: a migration must keep meaning what it meant when it
#: was written, whatever later happens to d8c3b1f04e75's module-level names.
_APPEND_ONLY: dict[str, str] = {
    "assistant_session": (
        "a session records what was served at one moment. A conversation "
        "progresses by INSERTing assistant_turn rows, and the outcome is "
        "derived from them"
    ),
    "assistant_turn": (
        "a turn is what one person was asked and answered. Correct it by "
        "INSERTing the next turn, which is what the conversation is"
    ),
    "justification": (
        "a justification is why somebody went ahead anyway. INSERT another one "
        "rather than rewriting what was said"
    ),
    "consumption_plan": (
        "a plan is what the requester committed to at reservation time. INSERT "
        "a superseding plan rather than editing the commitment"
    ),
    "quantity_suggestion": (
        "a suggestion records what was advised and what was kept. Both are "
        "evidence; neither is editable"
    ),
}


def _trigger(table: str, guidance: str) -> str:
    # INSTEAD OF, so the row is never touched; the check on `deleted` keeps a
    # statement that matches no rows harmless, like the Postgres row-level
    # trigger. Each CREATE TRIGGER must be alone in its batch, hence one
    # op.execute per table.
    message = f"{table} is append-only: {guidance}.".replace("'", "''")
    return f"""
CREATE TRIGGER {table}_no_update_or_delete
ON {table}
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM deleted)
        THROW 50001, '{message}', 1;
END
"""


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_bind().dialect.name == "mssql":
        for table, guidance in _APPEND_ONLY.items():
            op.execute(_trigger(table, guidance))


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "mssql":
        for table in _APPEND_ONLY:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_or_delete")
