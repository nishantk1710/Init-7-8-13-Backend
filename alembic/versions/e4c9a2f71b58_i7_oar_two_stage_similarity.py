"""I07 OAR two-stage similarity: embedding cache and explanations

oar-similarity-2. Purely additive -- one new table and five nullable columns --
so an upgrade cannot disturb a stored OAR result, and the recommendations stage
(which reads i7_oar_target) sees exactly the columns it always did.

* ``i7_oar_embedding`` keeps description embeddings between runs, keyed by a
  hash of (normalised description, model, model version, preprocessing
  version). A changed description or model is simply a different key, so the
  table never has to be invalidated -- only pruned of keys nothing uses.
* ``i7_oar_run.retrieval_config`` records how candidates were found.
* ``i7_oar_target.low_evidence_candidates`` / ``shortlisted_candidates`` record
  how many candidates the evidence rule refused and how many were scored
  exactly.
* ``i7_oar_neighbour.score_explanation`` records, per neighbour, which features
  entered the score, which could not be compared, the renormalised weights and
  the evidence verdict.

ROLLBACK drops the table and the columns. Nothing reads them but the OAR engine
itself; the loss is the cached embeddings (recomputed on the next run) and the
explanations of runs made while this was live.

Revision ID: e4c9a2f71b58
Revises: a7d2e94b1c35
Create Date: 2026-10-12 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e4c9a2f71b58"
down_revision: Union[str, Sequence[str], None] = "a7d2e94b1c35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "i7_oar_embedding",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("preprocessing_version", sa.String(length=32), nullable=False),
        sa.Column("text_hash", sa.String(length=32), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cache_key"),
    )
    op.create_index("ix_i7_oar_embedding_text_hash", "i7_oar_embedding", ["text_hash"])
    op.create_index(
        "ix_i7_oar_embedding_model",
        "i7_oar_embedding",
        ["model_name", "model_version", "preprocessing_version"],
    )

    op.add_column(
        "i7_oar_run", sa.Column("retrieval_config", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "i7_oar_target", sa.Column("low_evidence_candidates", sa.Integer(), nullable=True)
    )
    op.add_column(
        "i7_oar_target", sa.Column("shortlisted_candidates", sa.Integer(), nullable=True)
    )
    op.add_column(
        "i7_oar_neighbour",
        sa.Column("score_explanation", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("i7_oar_neighbour", "score_explanation")
    op.drop_column("i7_oar_target", "shortlisted_candidates")
    op.drop_column("i7_oar_target", "low_evidence_candidates")
    op.drop_column("i7_oar_run", "retrieval_config")
    op.drop_index("ix_i7_oar_embedding_model", table_name="i7_oar_embedding")
    op.drop_index("ix_i7_oar_embedding_text_hash", table_name="i7_oar_embedding")
    op.drop_table("i7_oar_embedding")
