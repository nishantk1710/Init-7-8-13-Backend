"""i8_coding_verdict

W5.5 / FR-2: keep the coding screen's model verdicts, so a restart or a SAP
reload does not throw them away and buy them again at six seconds each.

Deliberately NOT like i8_attestation. That table is immutable because it
records what a person judged; this records what a model said, and a better
model should be able to replace it -- so rows here are updated in place and
there is no trigger and no supersedes chain.

The unique index is the whole correctness rule: one verdict per material per
exact text. A verdict keyed on the material alone would survive somebody
re-wording the PO line, which is precisely the change the screen exists to
notice. See app/initiatives/i8/models.py:CodingVerdict.

Portable constructs only -- this creates identically on Postgres and Azure SQL.

Revision ID: a1c6f20d9b84
Revises: d5e1a7c40b92
Create Date: 2026-10-12 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c6f20d9b84'
down_revision: Union[str, Sequence[str], None] = 'd5e1a7c40b92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'i8_coding_verdict',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('material_id', sa.String(length=40), nullable=False),
        sa.Column('text_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('verdict', sa.String(length=32), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False,
                  server_default=''),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('model', sa.String(length=128), nullable=False,
                  server_default=''),
        sa.Column('provider', sa.String(length=32), nullable=False,
                  server_default=''),
        sa.Column('prompt_version', sa.Integer(), nullable=True),
        sa.Column('screened_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_i8_coding_verdict_material_id',
        'i8_coding_verdict', ['material_id'], unique=False,
    )
    op.create_index(
        'ix_i8_coding_verdict_text_fingerprint',
        'i8_coding_verdict', ['text_fingerprint'], unique=False,
    )
    op.create_index(
        'ix_i8_coding_verdict_verdict',
        'i8_coding_verdict', ['verdict'], unique=False,
    )
    op.create_index(
        'ix_i8_coding_verdict_screened_at',
        'i8_coding_verdict', ['screened_at'], unique=False,
    )
    # The rule, not an optimisation: one verdict per material per exact text.
    op.create_index(
        'uq_i8_coding_verdict_material_text',
        'i8_coding_verdict', ['material_id', 'text_fingerprint'], unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_i8_coding_verdict_material_text',
                  table_name='i8_coding_verdict')
    op.drop_index('ix_i8_coding_verdict_screened_at',
                  table_name='i8_coding_verdict')
    op.drop_index('ix_i8_coding_verdict_verdict',
                  table_name='i8_coding_verdict')
    op.drop_index('ix_i8_coding_verdict_text_fingerprint',
                  table_name='i8_coding_verdict')
    op.drop_index('ix_i8_coding_verdict_material_id',
                  table_name='i8_coding_verdict')
    op.drop_table('i8_coding_verdict')
