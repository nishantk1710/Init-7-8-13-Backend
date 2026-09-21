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

    # How many days open before a repair line moves to the next aging band.
    #
    # THIS IS A PLACEHOLDER, NOT A CONFIRMED RULE -- FRS open item 5, "aging
    # bands... to be tuned during real-data calibration." These are the values
    # the frontend already rendered before either side could read them from
    # configuration (see aging.py), kept as the default so nothing changes
    # until VZI gives an actual answer.
    #
    # Comma-separated ascending days, like series_prefixes -- the boundaries
    # between bands, not the bands themselves. Four boundaries make five bands;
    # changing the count changes how many bands exist, not just where they
    # fall. `app.initiatives.i8.aging.bucket_labels()` derives the labels.
    aging_band_boundaries: str = "15,30,45,60"

    # --- Criticality ------------------------------------------------------
    #
    # Deliberately NOT a setting here any more. Criticality moved to the shared
    # W3.4 module when it landed on 15-Sep, and it is configured once, globally,
    # by CRITICALITY_SOURCE in app.core.config -- not per initiative.
    #
    # An I8_CRITICALITY_SOURCE that only I08 obeyed would be a way to make one
    # initiative disagree with the other two about the same material, which is
    # the exact failure W3.4 exists to prevent. Read it through
    # app.shared.get_criticality_source(); /api/i8/config reports which source
    # actually answered.

    # --- Attestation (W5.3) -----------------------------------------------

    # The controlled list of fault categories the attestation form offers.
    #
    # Configuration, not a literal, and for the usual I08 reason: this is VZI's
    # vocabulary, not ours. It was not delivered with the extracts, so the list
    # below is a starting set drawn from the fault language already present in
    # the EKPO short texts. IT WILL CHANGE, and when it does it must be one
    # .env line rather than a code change, a review and a redeploy.
    #
    # Comma-separated, like series_prefixes, and for the same reason: a tuple
    # would be parsed as JSON by pydantic-settings and the honest one-value form
    # would become a startup error.
    fault_categories: str = (
        "BEARING_FAILURE,SEAL_LEAK,WEAR,IMPACT_DAMAGE,ELECTRICAL_FAULT,"
        "CORROSION,VIBRATION_DAMAGE,OVERHEATING,CONTAMINATION,UNKNOWN"
    )

    # How far from a repair line's raised date an attestation may sit and still
    # count as covering it.
    #
    # THIS IS A PROPOSAL, NOT A CONFIRMED RULE -- open question 3 on the task
    # plan. Material + plant + a date window is the only key both sides share:
    # an attestation is made against a physical part coming off a machine, and
    # nothing in that moment carries the purchase-order number it will later be
    # repaired under. A session id would match exactly and is not exposed.
    #
    # The window is symmetric around the line's raised date. The assessment
    # normally happens first -- the part is looked at, then the repair is
    # raised -- but not always, and a one-sided window would raise exceptions
    # against lines that were attested two days late. Symmetric is easier to
    # explain and easier to defend at UAT than a rule with a story attached.
    #
    # Every exception this produces carries the window that produced it, so a
    # change here is visible in the output rather than silently re-scoring the
    # queue.
    attestation_window_days: int = 30

    # The date the attestation control starts applying. Blank means "no cutover
    # is set", and every line is then judged as if the control had always
    # existed -- which is today's behaviour, unchanged.
    #
    # THE DATE ITSELF IS STILL MISSING. The RULING is not: asked on 20-Sep why
    # MISSING_ATTESTATION fires on all 1,225 lines, the team lead answered
    # "on them can we show before Spares Automation". A line raised before this
    # date did not fail a control -- the control did not exist -- so it is
    # labelled rather than accused, and kept out of the actionable count while
    # staying visible in the queue.
    #
    # Whoever owns go-live supplies the date; it is one .env line. Until then
    # this is deliberately blank rather than guessed, because a wrong cutover
    # silently forgives real misses on one side of it and accuses historical
    # lines on the other.
    attestation_cutover_date: str = ""

    # --- Coding candidates (W5.5) -----------------------------------------

    # The repair language that makes a PO line worth screening.
    #
    # Configuration, not a literal, for the usual reason: this is how VZI's
    # buyers write, not a rule we get to fix. Measured against the 82,718
    # free-text PO lines on 15-Sep, every one of these earns its place except
    # the last:
    #
    #     repair            271      refurb             24
    #     recon               3      service exchange    3
    #     overhaul            2      rebuild             2
    #     rotable             0      <- matches nothing in this extract
    #
    # `rotable` is kept deliberately. It is standard aviation/mining vocabulary
    # for exactly this class of part, it costs nothing to carry, and a keyword
    # that matches nothing today is evidence about the data rather than a bug.
    #
    # These are substring stems, matched case-insensitively: `repair` also
    # catches `repairs`, `repaired` and `plantrepairs`; `refurb` catches
    # `refurbished` and `refurbishment`. Measured: no false positives from
    # `recon` -- no `reconciliation` or `reconnect` leaked in.
    repair_language: str = (
        "repair,refurb,overhaul,recon,rebuild,service exchange,rotable"
    )

    # How sure the model has to be before a candidate counts as meeting the
    # bar, one of "low" / "medium" / "high".
    #
    # THIS IS A PLACEHOLDER, NOT A CONFIRMED RULE -- FRS open item 5,
    # "coding-candidate confidence threshold... to be tuned during real-data
    # calibration." Defaulting to "low" means every screened candidate meets
    # it today -- turning this into an actual filter is VZI's call, not ours.
    #
    # This never removes a candidate from the response (see
    # `app.initiatives.i8.coding_candidates.meets_confidence_threshold`): a
    # below-threshold candidate is still reported, only flagged, the same way
    # `actionableOnly` filters rather than silently drops.
    coding_candidate_confidence_threshold: str = "low"

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
    def repair_language_list(self) -> tuple[str, ...]:
        """The repair-language stems, parsed and lower-cased."""
        return tuple(
            word.strip().lower() for word in self.repair_language.split(",") if word.strip()
        )

    @property
    def repair_language_pattern(self) -> str:
        """The stems as one POSIX regex, for a database prefilter.

        Built from configuration and passed as a BIND PARAMETER, never
        interpolated into SQL. Each stem is regex-escaped, so a keyword
        containing a dot or a bracket narrows the search instead of silently
        becoming a wildcard.
        """
        import re as _re

        return "|".join(_re.escape(word) for word in self.repair_language_list)

    @property
    def fault_category_list(self) -> tuple[str, ...]:
        """The controlled fault-category list, parsed. Order is preserved
        because the form renders them in it."""
        return tuple(
            c.strip().upper() for c in self.fault_categories.split(",") if c.strip()
        )

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
    def aging_band_boundaries_list(self) -> tuple[int, ...]:
        """The aging-band day boundaries, parsed and ordered as configured."""
        return tuple(
            int(x.strip()) for x in self.aging_band_boundaries.split(",") if x.strip()
        )

    @property
    def attestation_cutover_date_value(self) -> date | None:
        """``attestation_cutover_date`` as a date, or None meaning "not set".

        Invalid input raises at startup, the same as ``reference_date`` -- a
        malformed cutover would otherwise decide, silently and wrongly, which
        historical lines get accused of a control failure.
        """
        if not self.attestation_cutover_date.strip():
            return None
        return date.fromisoformat(self.attestation_cutover_date.strip())

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
