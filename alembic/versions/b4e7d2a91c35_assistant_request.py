"""assistant_request, and assistant_session.request_id / line_no

Multi-material requests (app/assistant/requests.py). A request is the parent of
one ordinary assistant_session per material; each line is quoted on the SAP
item text as ``<request>/<n>``.

* ``assistant_request`` -- append-only like the rest of the spine, with the
  same trigger on Postgres (d8c3b1f04e75) and SQL Server (5c9e2a7d4f18).
* ``assistant_session.request_id`` / ``line_no`` -- nullable, and staying
  nullable: every session before this, and every stand-alone session after it,
  has neither. assistant_session is append-only, so nothing is backfilled.
* Two filtered unique indexes: one ``/n`` per line, and one line per
  material+plant, which is what lets SAP linking resolve request + material +
  plant to exactly one line. Filtered on ``request_id IS NOT NULL`` because SQL
  Server treats NULLs as equal in a unique index.

Revision ID: b4e7d2a91c35
Revises: d3f8a1c47b92
Create Date: 2026-10-16 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b4e7d2a91c35"
down_revision: Union[str, Sequence[str], None] = "d3f8a1c47b92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "assistant_request"
_GUIDANCE = (
    "a request records which materials were checked together and for whom. "
    "Its lines progress through their own sessions' turns"
)
_NOT_NULL = sa.text("request_id IS NOT NULL")


def _postgres_trigger() -> tuple[str, str]:
    function = f"""
CREATE OR REPLACE FUNCTION {_TABLE}_is_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '{_TABLE} is append-only: {_GUIDANCE}. (attempted % on id %)',
        TG_OP, OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""
    trigger = f"""
CREATE TRIGGER {_TABLE}_no_update_or_delete
BEFORE UPDATE OR DELETE ON {_TABLE}
FOR EACH ROW EXECUTE FUNCTION {_TABLE}_is_immutable();
"""
    return function, trigger


def _mssql_trigger() -> str:
    message = f"{_TABLE} is append-only: {_GUIDANCE}.".replace("'", "''")
    return f"""
CREATE TRIGGER {_TABLE}_no_update_or_delete
ON {_TABLE}
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
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=False),
        sa.Column("requested_for", sa.String(length=128), nullable=True),
        sa.Column("requester", sa.String(length=128), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_request")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_assistant_request_idempotency_key")),
    )
    op.create_index(op.f("ix_assistant_request_plant"), _TABLE, ["plant"])
    op.create_index(op.f("ix_assistant_request_requester"), _TABLE, ["requester"])
    op.create_index(op.f("ix_assistant_request_issued_at"), _TABLE, ["issued_at"])

    op.add_column("assistant_session", sa.Column("request_id", sa.String(length=32), nullable=True))
    op.add_column("assistant_session", sa.Column("line_no", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_assistant_session_request_id_assistant_request"),
        "assistant_session",
        _TABLE,
        ["request_id"],
        ["id"],
    )
    op.create_index(op.f("ix_assistant_session_request_id"), "assistant_session", ["request_id"])
    op.create_index(
        "uq_assistant_session_request_line",
        "assistant_session",
        ["request_id", "line_no"],
        unique=True,
        postgresql_where=_NOT_NULL,
        mssql_where=_NOT_NULL,
        sqlite_where=_NOT_NULL,
    )
    op.create_index(
        "uq_assistant_session_request_material",
        "assistant_session",
        ["request_id", "material_id", "plant"],
        unique=True,
        postgresql_where=_NOT_NULL,
        mssql_where=_NOT_NULL,
        sqlite_where=_NOT_NULL,
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        function, trigger = _postgres_trigger()
        op.execute(function)
        op.execute(trigger)
    elif dialect == "mssql":
        op.execute(_mssql_trigger())


def downgrade() -> None:
    """Downgrade schema."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_update_or_delete ON {_TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {_TABLE}_is_immutable()")
    elif dialect == "mssql":
        op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_update_or_delete")

    op.drop_index("uq_assistant_session_request_material", table_name="assistant_session")
    op.drop_index("uq_assistant_session_request_line", table_name="assistant_session")
    op.drop_index(op.f("ix_assistant_session_request_id"), table_name="assistant_session")
    op.drop_constraint(
        op.f("fk_assistant_session_request_id_assistant_request"), "assistant_session", type_="foreignkey"
    )
    op.drop_column("assistant_session", "line_no")
    op.drop_column("assistant_session", "request_id")

    op.drop_index(op.f("ix_assistant_request_issued_at"), table_name=_TABLE)
    op.drop_index(op.f("ix_assistant_request_requester"), table_name=_TABLE)
    op.drop_index(op.f("ix_assistant_request_plant"), table_name=_TABLE)
    op.drop_table(_TABLE)
