"""Configurable choices the extract adapter makes.

Separate from :mod:`app.initiatives.i7.policy` because these are *ingestion*
decisions -- which source records count as demand -- rather than calculation
rules. They still must not be hardcoded: the FRS calls the consumption
movement-type set "an unconfirmed business constant", which is precisely the
kind of value that, guessed, produces a full pipeline of confident wrong
numbers.

``candidate_movement_types`` therefore ships with the set the documents point at
but carries ``confirmed = False``, and the staging run records which set it used
so a later correction can be scoped exactly.
"""

from pydantic import BaseModel, ConfigDict, Field


class ConsumptionMovementPolicy(BaseModel):
    """Which SAP movement types count as demand.

    201/202 are cost-centre issues and their reversal; 261/262 are order issues
    and their reversal. Together they are what the Phase 0 audit measured
    consumption from, and they are what the Formula Reference's "consumption
    history" means.

    Unconfirmed. Goods issues to other destinations exist in the extract (311
    transfers, 641 deliveries, 543 subcontracting) and whether any belong in
    demand is a VZI question. Staging records the set used, so re-scoping later
    means re-running the adapter, not reinterpreting stored rows.
    """

    model_config = ConfigDict(frozen=True)

    issue_movement_types: tuple[str, ...] = ("201", "261")
    """Movement types that consume stock."""

    reversal_movement_types: tuple[str, ...] = ("202", "262")
    """Their reversals. Netted against issues in the same period rather than
    dropped -- a cancelled issue did not happen, and leaving it in overstates
    demand while dropping the pair silently loses the correction."""

    confirmed: bool = False
    """Whether VZI has ratified this set. ``False``."""

    @property
    def all_movement_types(self) -> tuple[str, ...]:
        return self.issue_movement_types + self.reversal_movement_types

    def signed_multiplier(self, movement_type: str) -> int:
        """``+1`` for an issue, ``-1`` for a reversal, ``0`` for anything else."""
        if movement_type in self.issue_movement_types:
            return 1
        if movement_type in self.reversal_movement_types:
            return -1
        return 0


class ExtractIngestionPolicy(BaseModel):
    """Everything the extract adapter needs configured."""

    model_config = ConfigDict(frozen=True)

    consumption: ConsumptionMovementPolicy = Field(default_factory=ConsumptionMovementPolicy)

    batch_size: int = Field(default=5000, ge=1)
    """Rows per insert batch. The raw tables hold 3.4M rows; streaming in
    batches keeps memory flat regardless of how large the extract grows."""
