"""criticality_report_upload, criticality_report_row and criticality_current

Monthly criticality reports (ZMM065), uploaded per plant from the Inventory
Planning "Criticality reports" page. Purely additive -- three new tables --
so an upgrade cannot disturb anything that exists. Nothing reads them until the
application's July baseline import has run (``python -m
app.integrations.criticality.baseline``, also run once at start-up), and until
then every reader reports criticality as unknown rather than failing.

The two history tables are append-only on SQL Server, like the assistant's:
a report is a record of what was uploaded and when. ``criticality_current`` is
derived and rebuilt per plant, so it is not.

ROLLBACK drops the three tables. The loss is the uploaded history; the July
delivery is still in ``raw_zmm065_*`` and is re-imported by the baseline.

Revision ID: c7a3f9e12d64
Revises: e4c9a2f71b58
Create Date: 2026-10-12 16:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a3f9e12d64"
down_revision: Union[str, Sequence[str], None] = "e4c9a2f71b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Copied, not imported: a migration must keep meaning what it meant when it
#: was written.
_APPEND_ONLY: dict[str, str] = {
    "criticality_report_upload": (
        "an upload records which file was loaded for which plant and month, by whom and when. "
        "A corrected file is a new upload, which replaces it for that month"
    ),
    "criticality_report_row": (
        "a row is what one uploaded report said about one material. "
        "Upload the corrected report instead of editing it"
    ),
}


def _trigger(table: str, guidance: str) -> str:
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
    op.create_table(
        "criticality_report_upload",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plant", sa.String(length=10), nullable=False),
        sa.Column("report_month", sa.Date(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("sheet_name", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by", sa.String(length=128), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("tier_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("blank_tiers", sa.Integer(), nullable=False),
        sa.Column("skipped_blank_material", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_criticality_report_upload")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_criticality_report_upload_dedupe_key")),
    )
    op.create_index(
        "ix_criticality_report_upload_plant_month", "criticality_report_upload", ["plant", "report_month"]
    )

    op.create_table(
        "criticality_report_row",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=False),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("material_raw", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=10), nullable=False),
        sa.Column("criticality", sa.String(length=16), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("mattyp", sa.String(length=10), nullable=True),
        sa.Column("mrp_type", sa.String(length=10), nullable=True),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["criticality_report_upload.id"],
            name=op.f("fk_criticality_report_row_upload_id_criticality_report_upload"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_criticality_report_row")),
    )
    op.create_index(op.f("ix_criticality_report_row_upload_id"), "criticality_report_row", ["upload_id"])

    op.create_table(
        "criticality_current",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("material_raw", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=10), nullable=False),
        sa.Column("criticality", sa.String(length=16), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("mattyp", sa.String(length=10), nullable=True),
        sa.Column("mrp_type", sa.String(length=10), nullable=True),
        sa.Column("tier_upload_id", sa.Integer(), nullable=True),
        sa.Column("tier_report_month", sa.Date(), nullable=True),
        sa.Column("listed_upload_id", sa.Integer(), nullable=False),
        sa.Column("listed_report_month", sa.Date(), nullable=False),
        sa.Column("carried_forward", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_criticality_current")),
        sa.UniqueConstraint("material", "plant", name="uq_criticality_current_material_plant"),
    )
    op.create_index(op.f("ix_criticality_current_plant"), "criticality_current", ["plant"])

    if op.get_bind().dialect.name == "mssql":
        for table, guidance in _APPEND_ONLY.items():
            op.execute(_trigger(table, guidance))


def downgrade() -> None:
    if op.get_bind().dialect.name == "mssql":
        for table in _APPEND_ONLY:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_or_delete")
    op.drop_index(op.f("ix_criticality_current_plant"), table_name="criticality_current")
    op.drop_table("criticality_current")
    op.drop_index(op.f("ix_criticality_report_row_upload_id"), table_name="criticality_report_row")
    op.drop_table("criticality_report_row")
    op.drop_index("ix_criticality_report_upload_plant_month", table_name="criticality_report_upload")
    op.drop_table("criticality_report_upload")
