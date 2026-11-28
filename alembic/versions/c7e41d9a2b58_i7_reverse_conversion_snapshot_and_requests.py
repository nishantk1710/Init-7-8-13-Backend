"""i7 Min-Max to OAR: the stored recommendation, its run, and the conversion requests with their approval ledger

Revision ID: c7e41d9a2b58
Revises: b5d2f8a14c93
Create Date: 2026-11-26 10:00:00.000000

New tables only -- nothing existing is altered. On SQL Server the approval ledger gets the same append-only
trigger the other approval-style ledgers use: UPDATE and DELETE are refused.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7e41d9a2b58"
down_revision: Union[str, Sequence[str], None] = "b5d2f8a14c93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEDGER = "i7_reverse_approval_ledger"
_TRIGGER_MESSAGE = (
    "i7_reverse_approval_ledger is append-only: an approval action is evidence. "
    "INSERT a new action rather than editing one."
)


def upgrade() -> None:
    op.create_table(
        "i7_reverse_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=True),
        sa.Column("anchor_month", sa.Date(), nullable=True),
        sa.Column("anchor_source", sa.String(length=32), nullable=True),
        sa.Column("latest_staged_month", sa.Date(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("source_population", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("excluded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("duplicate_keys", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_written", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("excluded_by_plant", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_i7_reverse_run_status", "i7_reverse_run", ["status"])

    op.create_table(
        "i7_reverse_recommendation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("sap_material_number", sa.String(length=40), nullable=False),
        sa.Column("sap_plant_code", sa.String(length=8), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("mrp_type", sa.String(length=8), nullable=True),
        sa.Column("criticality", sa.String(length=16), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=48), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=False),
        sa.Column("decision_rule", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=True),
        sa.Column("points", sa.Text(), nullable=True),
        sa.Column("issues_latest_12m", sa.Integer(), nullable=False),
        sa.Column("months_with_use_12m", sa.Integer(), nullable=False),
        sa.Column("issues_prior_12m", sa.Integer(), nullable=False),
        sa.Column("first_valid_month", sa.Date(), nullable=True),
        sa.Column("last_valid_month", sa.Date(), nullable=True),
        sa.Column("months_since_last_movement", sa.Integer(), nullable=True),
        sa.Column("history_span_months", sa.Integer(), nullable=True),
        sa.Column("lifetime_months_with_use", sa.Integer(), nullable=False),
        sa.Column("lead_time_days", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("lead_time_bucket", sa.String(length=8), nullable=False),
        sa.Column("current_stock", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("price_currency", sa.String(length=8), nullable=True),
        sa.Column("value_currency", sa.String(length=8), nullable=True),
        sa.Column("stock_value", sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column("minbe", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("mabst", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sap_material_number", "sap_plant_code", name="uq_i7_reverse_rec_run_key"),
    )
    op.create_index("ix_i7_reverse_recommendation_run_id", "i7_reverse_recommendation", ["run_id"])
    op.create_index("ix_i7_reverse_rec_run_order", "i7_reverse_recommendation", ["run_id", "sort_order"])

    op.create_table(
        "i7_planning_group_observation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sap_material_number", sa.String(length=40), nullable=False),
        sa.Column("sap_plant_code", sa.String(length=8), nullable=False),
        sa.Column("planning_group", sa.String(length=8), nullable=False),
        sa.Column("first_seen_on", sa.Date(), nullable=False),
        sa.Column("changed_on", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sap_material_number", "sap_plant_code", name="uq_i7_planning_group_key"),
    )
    op.create_index("ix_i7_planning_group_observation_sap_material_number", "i7_planning_group_observation", ["sap_material_number"])

    op.create_table(
        "i7_reverse_conversion_request",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sap_material_number", sa.String(length=40), nullable=False),
        sa.Column("sap_plant_code", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pending_role", sa.String(length=32), nullable=True),
        sa.Column("chain_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("adjustment_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("anchor_month", sa.Date(), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=True),
        sa.Column("reason", sa.String(length=48), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=False),
        sa.Column("stock_value", sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column("submitted_by", sa.String(length=64), nullable=False),
        sa.Column("submit_comment", sa.String(length=1000), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_i7_reverse_conversion_request_status", "i7_reverse_conversion_request", ["status"])
    op.create_index(
        "ix_i7_reverse_request_key", "i7_reverse_conversion_request", ["sap_material_number", "sap_plant_code", "id"]
    )

    op.create_table(
        _LEDGER,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=False),
        sa.Column("new_status", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_i7_reverse_ledger_request", _LEDGER, ["request_id", "timestamp"])

    if op.get_bind().dialect.name == "mssql":
        message = _TRIGGER_MESSAGE.replace("'", "''")
        op.execute(
            f"""
CREATE TRIGGER {_LEDGER}_no_update_or_delete
ON {_LEDGER}
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM deleted)
        THROW 50001, '{message}', 1;
END
"""
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "mssql":
        op.execute(f"DROP TRIGGER IF EXISTS {_LEDGER}_no_update_or_delete")
    op.drop_index("ix_i7_planning_group_observation_sap_material_number", table_name="i7_planning_group_observation")
    op.drop_table("i7_planning_group_observation")
    op.drop_index("ix_i7_reverse_ledger_request", table_name=_LEDGER)
    op.drop_table(_LEDGER)
    op.drop_index("ix_i7_reverse_request_key", table_name="i7_reverse_conversion_request")
    op.drop_index("ix_i7_reverse_conversion_request_status", table_name="i7_reverse_conversion_request")
    op.drop_table("i7_reverse_conversion_request")
    op.drop_index("ix_i7_reverse_rec_run_order", table_name="i7_reverse_recommendation")
    op.drop_index("ix_i7_reverse_recommendation_run_id", table_name="i7_reverse_recommendation")
    op.drop_table("i7_reverse_recommendation")
    op.drop_index("ix_i7_reverse_run_status", table_name="i7_reverse_run")
    op.drop_table("i7_reverse_run")
