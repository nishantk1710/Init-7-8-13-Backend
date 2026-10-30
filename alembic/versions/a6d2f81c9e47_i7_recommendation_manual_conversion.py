"""i7 recommendation: manual Min-Max conversion request

Revision ID: a6d2f81c9e47
Revises: f4b8d2a71c63
Create Date: 2026-10-30 15:00:00.000000

A user can ask to convert a NOT_ELIGIBLE OAR material to Min-Max ordering. The request goes through the
existing approval chain; these columns record that it was a manual request, by whom and when, without
touching ``conversion_eligibility`` (the rule's verdict). Existing rows get ``manually_converted = 0`` and
NULL for the other two: none of them was a manual request, and nothing about them changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6d2f81c9e47'
down_revision: Union[str, Sequence[str], None] = 'f4b8d2a71c63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'i7_recommendation',
        sa.Column('manually_converted', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column('i7_recommendation', sa.Column('requested_by', sa.String(length=64), nullable=True))
    op.add_column('i7_recommendation', sa.Column('requested_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('i7_recommendation', 'requested_at')
    op.drop_column('i7_recommendation', 'requested_by')
    if op.get_bind().dialect.name == 'mssql':
        # SQL Server will not drop a column that still has its (system-named) DEFAULT constraint.
        op.execute(
            """
            DECLARE @constraint sysname;
            SELECT @constraint = dc.name
              FROM sys.default_constraints dc
              JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
             WHERE dc.parent_object_id = OBJECT_ID('i7_recommendation') AND c.name = 'manually_converted';
            IF @constraint IS NOT NULL EXEC('ALTER TABLE i7_recommendation DROP CONSTRAINT ' + @constraint);
            """
        )
    op.drop_column('i7_recommendation', 'manually_converted')
