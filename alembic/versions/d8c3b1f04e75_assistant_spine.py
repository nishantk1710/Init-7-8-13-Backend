"""assistant_spine -- W7 shared session, turns, justification, plan, suggestion

Revision ID: d8c3b1f04e75
Revises: a868f8b8ac1a, b2f4c81d5a37
Create Date: 2026-09-21

This migration does two jobs, and the second one was not planned.

1. It creates the five tables the reservation-time assistant writes to.

2. **It is a merge revision.** The I08/I13 merge left the migration history with
   two heads -- ``b2f4c81d5a37`` (i8_attestation) and ``a868f8b8ac1a``
   (i13_act_exceptions), both descending from ``770e63ee2f68`` down separate
   chains. Alembic refuses ``upgrade head`` while that is true:

       Multiple head revisions are present for given argument 'head'

   So on the merged branch the database could not be brought up to date at all,
   and a local database sitting on ``b2f4c81d5a37`` had never received any of
   the four I13 migrations. Giving this revision both heads as its parents
   resolves that, because WS7 genuinely depends on both lineages -- it reads
   I08's register and I13's WATCH mart -- so the merge is not a bookkeeping
   convenience, it is the truth about the dependency.

Append-only, enforced here
---------------------------
Every table gets the same trigger ``i8_attestation`` has. The reasoning is
unchanged and worth repeating rather than cross-referencing: these are audit
records, rewriting one does not correct history but destroys it, and "the
service layer never issues an UPDATE" is a convention that is one careless
session away from being untrue.

The trigger is Postgres-only, deliberately, exactly as the attestation's is. The
*tables* are portable; the guarantee is enforced in the service layer and proven
by tests, so it holds on Azure SQL too. The trigger is defence in depth where
the engine supports it, and is skipped rather than failing the migration where
it does not.

Why there is no ``status`` column on a session
-----------------------------------------------
Because the table is append-only, there is nowhere to write "completed" later --
so a session's outcome is derived from its turns instead. That is a deliberate
consequence and not an omission; see ``app/assistant/models.py``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8c3b1f04e75"
down_revision: Union[str, Sequence[str], None] = ("a868f8b8ac1a", "b2f4c81d5a37")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every append-only table this migration creates, and the sentence its trigger
#: raises. The message names the table and says what to do instead -- an error
#: that only says "not allowed" sends somebody to read the source.
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


def _immutability_sql(table: str, guidance: str) -> tuple[str, str]:
    # TG_OP names the operation that was attempted -- "attempted UPDATE on id
    # X" rather than the bare "attempted %" the i8_attestation trigger emits,
    # which passes a literal per cent where the verb should be. That one is
    # already applied and is not worth an edit to migration history; this one
    # is new, so it says the useful thing.
    function = f"""
CREATE OR REPLACE FUNCTION {table}_is_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        '{table} is append-only: {guidance}. (attempted % on id %)',
        TG_OP, OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""
    trigger = f"""
CREATE TRIGGER {table}_no_update_or_delete
BEFORE UPDATE OR DELETE ON {table}
FOR EACH ROW EXECUTE FUNCTION {table}_is_immutable();
"""
    return function, trigger


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assistant_session",
        # The primary key IS the session ID the requester types into SAP. One
        # namespace, no surrogate key -- see app/assistant/ids.py for why the
        # format is ten characters and checksummed.
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("flow", sa.String(length=8), nullable=False),
        # Normalised before storing -- ruling 5.1, same as i8_attestation.
        sa.Column("material_id", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(precision=18, scale=3), nullable=True),
        # The routing inputs, not just the answer -- W7.1 decided this from
        # configuration and from MARC, and both can change afterwards.
        sa.Column("eighty_series", sa.Boolean(), nullable=False),
        sa.Column("material_scope", sa.String(length=16), nullable=False),
        sa.Column("mrp_type", sa.String(length=8), nullable=True),
        sa.Column("routing_reason", sa.Text(), nullable=False),
        sa.Column("requester", sa.String(length=128), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        # Advisory. Never used to invalidate a session retrospectively: the
        # reservation is already in SAP and the platform cannot write back.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # The advice AS SERVED, as a JSON string in a portable Text column.
        # Nothing queries into it; it is read back whole, months later.
        sa.Column("assessment", sa.Text(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("narrative_prompt_id", sa.String(length=64), nullable=True),
        sa.Column("narrative_prompt_version", sa.Integer(), nullable=True),
        sa.Column("narrative_model", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_session")),
    )
    op.create_index(op.f("ix_assistant_session_flow"), "assistant_session", ["flow"])
    op.create_index(
        op.f("ix_assistant_session_material_id"), "assistant_session", ["material_id"]
    )
    op.create_index(op.f("ix_assistant_session_plant"), "assistant_session", ["plant"])
    op.create_index(
        op.f("ix_assistant_session_requester"), "assistant_session", ["requester"]
    )
    op.create_index(
        op.f("ix_assistant_session_issued_at"), "assistant_session", ["issued_at"]
    )
    # The FR-8 access pattern: every session for this part at this plant.
    op.create_index(
        "ix_assistant_session_material_plant",
        "assistant_session",
        ["material_id", "plant"],
    )

    op.create_table(
        "assistant_turn",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("step_kind", sa.String(length=16), nullable=False),
        # The question AS ASKED. The script will change; what this person was
        # asked will not.
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column(
            "answered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["assistant_session.id"],
            name=op.f("fk_assistant_turn_session_id_assistant_session"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_turn")),
    )
    op.create_index(
        op.f("ix_assistant_turn_session_id"), "assistant_turn", ["session_id"]
    )
    # UNIQUE, and that is the concurrency guard: two answers racing to the same
    # question cannot both be recorded as though they were a dialogue.
    op.create_index(
        "uq_assistant_turn_session_sequence",
        "assistant_turn",
        ["session_id", "sequence"],
        unique=True,
    )

    op.create_table(
        "justification",
        sa.Column("id", sa.String(length=32), nullable=False),
        # Nullable: a justification can also answer an ACT exception that no
        # session produced.
        sa.Column("session_id", sa.String(length=32), nullable=True),
        # Deliberately NOT a foreign key -- i13_act_exception is rebuilt by
        # detection on a deterministic business id, and a constraint here would
        # let this table dictate what detection may re-derive.
        sa.Column("exception_id", sa.String(length=120), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("reason_category", sa.String(length=64), nullable=False),
        sa.Column("free_text", sa.Text(), nullable=False),
        sa.Column("material_id", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=False),
        sa.Column("author", sa.String(length=128), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["assistant_session.id"],
            name=op.f("fk_justification_session_id_assistant_session"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_justification")),
    )
    op.create_index(op.f("ix_justification_session_id"), "justification", ["session_id"])
    op.create_index(
        op.f("ix_justification_exception_id"), "justification", ["exception_id"]
    )
    op.create_index(op.f("ix_justification_kind"), "justification", ["kind"])
    op.create_index(
        op.f("ix_justification_reason_category"), "justification", ["reason_category"]
    )
    op.create_index(
        op.f("ix_justification_material_id"), "justification", ["material_id"]
    )
    op.create_index(op.f("ix_justification_plant"), "justification", ["plant"])
    op.create_index(op.f("ix_justification_author"), "justification", ["author"])
    op.create_index(
        op.f("ix_justification_recorded_at"), "justification", ["recorded_at"]
    )

    op.create_table(
        "consumption_plan",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        # Both null at capture, and that is the NORMAL case: the assistant runs
        # while the reservation is being created, so it has no number yet. The
        # link is made afterwards by FR-8, which needs Bednr (blocker B2).
        sa.Column("reservation_number", sa.String(length=20), nullable=True),
        sa.Column("reservation_item", sa.String(length=8), nullable=True),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("planned_quantity", sa.Numeric(precision=18, scale=3), nullable=False),
        # A WINDOW, per FR-2(b) -- not the single planned_use_date today's ACT
        # detection reads. Captured in full and read narrowly; see the module
        # docstring on ConsumptionPlanRecord for why that was the choice.
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        # "Where known" -- never inferred. No EKKN/AUFK source is loaded.
        sa.Column("cost_centre", sa.String(length=32), nullable=True),
        sa.Column("order_number", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("captured_by", sa.String(length=128), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["assistant_session.id"],
            name=op.f("fk_consumption_plan_session_id_assistant_session"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consumption_plan")),
    )
    op.create_index(
        op.f("ix_consumption_plan_session_id"), "consumption_plan", ["session_id"]
    )
    op.create_index(
        op.f("ix_consumption_plan_reservation_number"),
        "consumption_plan",
        ["reservation_number"],
    )
    op.create_index(
        op.f("ix_consumption_plan_material"), "consumption_plan", ["material"]
    )
    op.create_index(op.f("ix_consumption_plan_plant"), "consumption_plan", ["plant"])
    op.create_index(op.f("ix_consumption_plan_status"), "consumption_plan", ["status"])
    op.create_index(
        op.f("ix_consumption_plan_captured_by"), "consumption_plan", ["captured_by"]
    )
    op.create_index(
        op.f("ix_consumption_plan_captured_at"), "consumption_plan", ["captured_at"]
    )
    op.create_index(
        "ix_consumption_plan_material_plant",
        "consumption_plan",
        ["material", "plant"],
    )

    op.create_table(
        "quantity_suggestion",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("material", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(precision=18, scale=3), nullable=False),
        # NULL where no suggestion was made -- too little history to average
        # over. Null is not zero: "we suggest nothing" and "we suggest none"
        # are opposite instructions.
        sa.Column("suggested_quantity", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("accepted_quantity", sa.Numeric(precision=18, scale=3), nullable=False),
        sa.Column("suggestion_reason", sa.Text(), nullable=False),
        # The arithmetic's inputs, so the number can be re-derived exactly --
        # including the three configured values, which are OUR defaults until
        # VZI confirms them (open question 10).
        sa.Column("stock_on_hand", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("open_po_quantity", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column(
            "average_monthly_consumption", sa.Numeric(precision=18, scale=3), nullable=True
        ),
        sa.Column("months_of_cover", sa.Numeric(precision=18, scale=3), nullable=True),
        sa.Column("cover_ceiling_months", sa.Numeric(precision=18, scale=3), nullable=False),
        sa.Column("lookback_months", sa.Integer(), nullable=False),
        sa.Column("min_history_consumptions", sa.Integer(), nullable=False),
        sa.Column("consumption_count", sa.Integer(), nullable=False),
        sa.Column(
            "suggested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["assistant_session.id"],
            name=op.f("fk_quantity_suggestion_session_id_assistant_session"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quantity_suggestion")),
    )
    op.create_index(
        op.f("ix_quantity_suggestion_session_id"), "quantity_suggestion", ["session_id"]
    )
    op.create_index(
        op.f("ix_quantity_suggestion_material"), "quantity_suggestion", ["material"]
    )
    op.create_index(
        op.f("ix_quantity_suggestion_plant"), "quantity_suggestion", ["plant"]
    )
    op.create_index(
        op.f("ix_quantity_suggestion_suggested_at"),
        "quantity_suggestion",
        ["suggested_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        for table, guidance in _APPEND_ONLY.items():
            function, trigger = _immutability_sql(table, guidance)
            op.execute(function)
            op.execute(trigger)


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "postgresql":
        for table in _APPEND_ONLY:
            op.execute(
                f"DROP TRIGGER IF EXISTS {table}_no_update_or_delete ON {table}"
            )
            op.execute(f"DROP FUNCTION IF EXISTS {table}_is_immutable()")

    op.drop_table("quantity_suggestion")
    op.drop_table("consumption_plan")
    op.drop_table("justification")
    op.drop_table("assistant_turn")
    op.drop_table("assistant_session")
