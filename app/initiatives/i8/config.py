"""Initiative 08 settings -- every business rule as configuration.

Nothing in I08 hard-codes a repair convention, and there is a test that keeps it
that way (``tests/test_i8_config.py``). The reason is specific, not stylistic:

    "Repair-PO filter proposed on evidence as item category 3 corroborated by
     document type ZREP, PENDING SAP TEAM CONFIRMATION."
                                             -- VZI_AI_Dev_Plan.xlsx, W5.2

The evidence is strong (item category 3 appears on ZREP documents and nowhere
else across 62,000+ other lines, and all 1,225 of those lines are on 80-series
materials) but it is still a proposal. If the SAP team comes back and says the
value is 'L', that must be one line in ``.env`` -- not a code change, a review
and a redeploy.

Every field here is read from the environment with an ``I8_`` prefix, falling
back to ``.env``, exactly like ``app.core.config.Settings``.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class I8Settings(BaseSettings):
    """Initiative 08 rules. Field names map to ``I8_``-prefixed env vars."""

    model_config = SettingsConfigDict(
        env_prefix="I8_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 80-series detection (W5.1) --------------------------------------

    # Comma-separated, like FRONTEND_ORIGIN in the core settings. A plain string
    # rather than a tuple because pydantic-settings parses complex types as
    # JSON, which would make the honest `I8_SERIES_PREFIXES=80` a startup error.
    # VZI could add a second repairable series without touching any code.
    series_prefixes: str = "80"

    # A repairable material number is exactly this long once normalised. The
    # guard that stops '80' and other short numbers matching the prefix.
    material_number_length: int = 10

    # --- Repair-PO identification (W5.2) ---------------------------------

    # PSTYP. The filter. Pending SAP team confirmation -- see the module note.
    repair_item_category: str = "3"

    # BSART. Corroborating attribute ONLY, never a gate: 455 of the 1,225
    # repair lines have no EKKO header in this extract, and inner-joining to
    # get this value silently drops 37% of the register.
    repair_doc_type: str = "ZREP"

    # --- Movement conventions (W5.2 lifecycle) ---------------------------
    #
    # Standard SAP movement types rather than VZI conventions, so these are far
    # less likely to move than the two above. They are still settings and not
    # literals, because the register reads each of them in more than one place
    # and a movement type buried in a SQL string is the kind of thing that gets
    # changed in four files and missed in the fifth.
    #
    # Every one is confirmed present in the extract -- this is the flow the data
    # actually shows, not a theoretical one.
    gr_movement_type: str = "101"
    """Goods receipt. The repaired unit coming back."""

    gr_reversal_movement_type: str = "102"
    """Reversal of a goods receipt. Netted off, never counted as a return."""

    gr_history_category: str = "E"
    """EKBE history category for goods receipts. 'Q' is invoices -- not a return."""

    dispatch_movement_type: str = "541"
    """Transfer to vendor stock. The unit physically leaving for repair."""

    vendor_special_stock: str = "O"
    """Special-stock indicator for vendor-held stock.

    A 541 posts TWO rows: the vendor side (SOBKZ 'O', SHKZG 'S') and the plant
    side (SOBKZ null, SHKZG 'H'). Picking the vendor side is what stops every
    dispatch quantity being counted twice.
    """

    # --- Aging and overdue (W5.2) ----------------------------------------

    # Days past the schedule-line delivery date before a line counts as overdue.
    # The plan calls this "configurable grace" in as many words.
    overdue_grace_days: int = 7

    # The date all aging is measured from. Blank means "today".
    #
    # Set it for a demo or a UAT pack: the extract is a July-2026 snapshot, so
    # by wall clock every open line is already weeks old and every number moves
    # each morning. Pinning it makes output explainable and stops tests being
    # time bombs that pass this week and fail next.
    reference_date: str = ""

    # --- Criticality (W3.4 seam) -----------------------------------------

    # Where criticality comes from. 'zmm065' reads the aging report today;
    # switch to 'w34' when Nishant's criticality module lands behind the
    # adapter in criticality.py. Nothing above the adapter changes.
    criticality_source: str = "zmm065"

    # --- Presentation -----------------------------------------------------

    # Plant code -> display name, comma separated.
    #
    # No plant-name table was delivered, so these are not read from SAP. They
    # come from app/seed/manifest.py, which records 1300 as Black Mountain and
    # 1500 as Gamsberg -- documented facts about this landscape rather than
    # invented labels. A code with no entry here is displayed as its code,
    # never as a guess.
    # Only 1300 and 1500 are listed because only those two are documented.
    # Plants 1200, 2000, 3000 and 1600 appear in the stock data and are served
    # as their codes until someone supplies their names.
    plant_names: str = "1300=Black Mountain,1500=Gamsberg"

    # --- API paging -------------------------------------------------------
    # 781 open repair lines will not render in one response.
    default_page_size: int = 50
    max_page_size: int = 500

    @property
    def series_prefix_list(self) -> tuple[str, ...]:
        """The 80-series prefixes, parsed. Mirrors Settings.cors_origins."""
        return tuple(p.strip() for p in self.series_prefixes.split(",") if p.strip())

    @property
    def plant_name_map(self) -> dict[str, str]:
        """Plant code -> display name. Missing codes fall back to the code."""
        mapping: dict[str, str] = {}
        for entry in self.plant_names.split(","):
            code, _, name = entry.partition("=")
            if code.strip() and name.strip():
                mapping[code.strip()] = name.strip()
        return mapping

    @property
    def reference_date_value(self) -> date | None:
        """``reference_date`` as a date, or None meaning "use today".

        Invalid input raises here, at startup, rather than producing silently
        wrong aging on every row of every response.
        """
        if not self.reference_date.strip():
            return None
        return date.fromisoformat(self.reference_date.strip())


@lru_cache
def get_i8_settings() -> I8Settings:
    """The process-wide I08 settings instance."""
    return I8Settings()


def reset_i8_settings_cache() -> None:
    """Drop the cached settings. For tests that vary the rules."""
    get_i8_settings.cache_clear()
