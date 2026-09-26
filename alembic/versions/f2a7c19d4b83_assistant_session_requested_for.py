"""assistant_session.requested_for -- who the part is actually for

Revision ID: f2a7c19d4b83
Revises: e1f4a90c72b6
Create Date: 2026-09-23

Two people, two columns
------------------------
One person sits and runs the assistant for everybody. They are not the person
who wants the part. Until this column existed there was nowhere to record the
difference, so a session said only who had operated the tool -- which, when one
coordinator operates it for the whole site, is the same answer every time and
therefore no answer at all.

``requester`` keeps holding the operator: server-set from the caller, never read
from the request body, exactly as ``i8_attestation.attestor`` is. This column
holds the name the operator **typed**, which is a different kind of value and is
deliberately named so that nothing reads one where it meant the other. A single
character between ``requester`` and ``requested_by`` would have been an
invitation to mix them up in review, so the name is ``requested_for``.

What this column is NOT
------------------------
It is not identity and it must never be treated as identity. Nobody verified it;
one person typed another person's name into a text box. It answers "who is this
reservation for", which is a property of the reservation, in the same way the
material number is. When Entra sign-in lands it does not become authenticated --
it stays a typed name, and ``requester`` becomes the real one.

Why nullable, and staying nullable
-----------------------------------
Same reasoning as ``department`` one revision back. ``assistant_session`` is
append-only -- a Postgres trigger blocks UPDATE -- so existing rows cannot be
backfilled by this migration or by anything else, and a NOT NULL column would
need a default, which means inventing a name for every session minted before
anybody was asked for one.

The BAdI deep link does not carry a name either. A session opened from SAP
legitimately has none, and NULL is the honest record of that rather than a
temporary state to be tightened later.

Nothing is dropped here
------------------------
``requested_quantity`` stops being collected at the entry point in this change
and the column stays, for the reason the previous revision already gave: on an
append-only audit table a dropped column is history destroyed, not a field
tidied away.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a7c19d4b83"
down_revision: Union[str, Sequence[str], None] = "e1f4a90c72b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "assistant_session",
        sa.Column("requested_for", sa.String(length=128), nullable=True),
    )
    # Indexed for the same reason department is: "every session raised for this
    # person" is a filter over the whole log rather than a lookup of one row,
    # and it is now the only column that can answer it -- requester says
    # "the coordinator" on every row.
    #
    # 128 matches requester rather than department's 64. These hold names of
    # the same kind of thing, and a typed name truncating at a different length
    # from a recorded one would be an arbitrary difference to explain later.
    op.create_index(
        op.f("ix_assistant_session_requested_for"),
        "assistant_session",
        ["requested_for"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_assistant_session_requested_for"), table_name="assistant_session"
    )
    op.drop_column("assistant_session", "requested_for")
