"""i8_attestation append-only trigger on SQL Server

b2f4c81d5a37 created the immutability trigger on Postgres only, so on Azure SQL
an attestation -- an audit record -- could be edited or deleted silently. This
adds the same guarantee there: same trigger name, same message.

Revision ID: e3b7c2d9f104
Revises: af8f9acb958a
Create Date: 2026-09-26 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e3b7c2d9f104"
down_revision: Union[str, Sequence[str], None] = "af8f9acb958a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# INSTEAD OF, so the row is never touched; the check on `deleted` keeps a
# statement that matches no rows harmless, like the Postgres row-level trigger.
# Same name as on Postgres, so `ALTER TABLE ... DISABLE TRIGGER
# i8_attestation_no_update_or_delete` works on both.
_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER i8_attestation_no_update_or_delete
ON i8_attestation
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM deleted)
        THROW 50001,
            'i8_attestation is append-only: attestations are audit records. To correct one, INSERT a new row with supersedes = <the original id>, which keeps the original readable.',
            1;
END
"""


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_bind().dialect.name == "mssql":
        op.execute(_IMMUTABILITY_TRIGGER)


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "mssql":
        op.execute("DROP TRIGGER IF EXISTS i8_attestation_no_update_or_delete")
