"""i8_attestation -- W5.3 condition-to-repair attestation

Revision ID: b2f4c81d5a37
Revises: 770e63ee2f68
Create Date: 2026-09-15

The first table Initiative 08 writes to. Everything before this was a read model
over the July extract.

The interesting part of this migration is not the table, it is the trigger. An
attestation is an audit record: rewriting one does not correct history, it
destroys it. The service layer never issues an UPDATE, but "the service layer
does not do that" is a convention, and conventions are one careless session away
from being untrue. The trigger makes it an error.

**The trigger is Postgres-only, deliberately.** ``app/models/base.py`` requires
portable constructs in models because the deployed database is not guaranteed to
be this engine. That rule is respected: the *table* is portable, and the
immutability guarantee is enforced in the service layer and proven by tests, so
it holds on any engine. The trigger is defence in depth where the engine
supports it, and is skipped rather than failing the migration where it does not.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2f4c81d5a37"
down_revision: Union[str, Sequence[str], None] = "770e63ee2f68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# An amendment is a new row pointing at the one it supersedes. Editing or
# deleting a row is always a mistake, so the database says so out loud rather
# than letting it succeed quietly.
_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION i8_attestation_is_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'i8_attestation is append-only: attestations are audit records. '
        'To correct one, INSERT a new row with supersedes = %, which keeps '
        'the original readable. (attempted %% on id %)',
        OLD.id, OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

_IMMUTABILITY_TRIGGER = """
CREATE TRIGGER i8_attestation_no_update_or_delete
BEFORE UPDATE OR DELETE ON i8_attestation
FOR EACH ROW EXECUTE FUNCTION i8_attestation_is_immutable();
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "i8_attestation",
        sa.Column("id", sa.String(length=32), nullable=False),
        # Normalised before storing -- ruling 5.1. An attestation typed against
        # the zero-padded form and a repair line read as the stripped form are
        # about the same part.
        sa.Column("material_id", sa.String(length=40), nullable=False),
        sa.Column("plant", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=3), nullable=False),
        sa.Column("serial_number", sa.String(length=64), nullable=True),
        sa.Column("condition_description", sa.Text(), nullable=False),
        sa.Column("fault_category", sa.String(length=64), nullable=False),
        sa.Column("recommendation", sa.String(length=32), nullable=False),
        # A reference string only. File upload is descoped; SharePoint is not
        # provisioned. The platform does not pretend to hold the artefact.
        sa.Column("evidence_reference", sa.String(length=500), nullable=True),
        sa.Column("attestor", sa.String(length=128), nullable=False),
        sa.Column(
            "attested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Always null today. FR-8 session linkage is not I08's scope; the column
        # exists so adding it later is not a migration on audit history.
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("supersedes", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["supersedes"],
            ["i8_attestation.id"],
            name=op.f("fk_i8_attestation_supersedes_i8_attestation"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i8_attestation")),
    )
    op.create_index(
        op.f("ix_i8_attestation_attestor"), "i8_attestation", ["attestor"], unique=False
    )
    op.create_index(
        op.f("ix_i8_attestation_attested_at"),
        "i8_attestation",
        ["attested_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_i8_attestation_fault_category"),
        "i8_attestation",
        ["fault_category"],
        unique=False,
    )
    op.create_index(
        op.f("ix_i8_attestation_material_id"),
        "i8_attestation",
        ["material_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_i8_attestation_plant"), "i8_attestation", ["plant"], unique=False
    )
    op.create_index(
        op.f("ix_i8_attestation_recommendation"),
        "i8_attestation",
        ["recommendation"],
        unique=False,
    )
    op.create_index(
        op.f("ix_i8_attestation_supersedes"),
        "i8_attestation",
        ["supersedes"],
        unique=False,
    )
    # The exception check's access pattern: "is there an attestation for this
    # material at this plant, and when?", once per repair line per refresh.
    op.create_index(
        "ix_i8_attestation_material_plant_at",
        "i8_attestation",
        ["material_id", "plant", "attested_at"],
        unique=False,
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(_IMMUTABILITY_FUNCTION)
        op.execute(_IMMUTABILITY_TRIGGER)


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS i8_attestation_no_update_or_delete "
            "ON i8_attestation"
        )
        op.execute("DROP FUNCTION IF EXISTS i8_attestation_is_immutable()")

    op.drop_index("ix_i8_attestation_material_plant_at", table_name="i8_attestation")
    op.drop_index(op.f("ix_i8_attestation_supersedes"), table_name="i8_attestation")
    op.drop_index(op.f("ix_i8_attestation_recommendation"), table_name="i8_attestation")
    op.drop_index(op.f("ix_i8_attestation_plant"), table_name="i8_attestation")
    op.drop_index(op.f("ix_i8_attestation_material_id"), table_name="i8_attestation")
    op.drop_index(op.f("ix_i8_attestation_fault_category"), table_name="i8_attestation")
    op.drop_index(op.f("ix_i8_attestation_attested_at"), table_name="i8_attestation")
    op.drop_index(op.f("ix_i8_attestation_attestor"), table_name="i8_attestation")
    op.drop_table("i8_attestation")
