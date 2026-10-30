"""Initiative 08 settings -- every business rule as configuration.

Nothing in I08 hard-codes a repair convention, and there is a test that keeps it
that way (``tests/test_i8_config.py``). The reason is specific, not stylistic:

    "Repair-PO filter proposed on evidence as item category 3 corroborated by
     document type ZREP, PENDING SAP TEAM CONFIRMATION."
                                             -- VZI_AI_Dev_Plan.xlsx, W5.2

The evidence was always strong (item category 3 appears on ZREP documents and
nowhere else across 62,000+ other lines, and all 1,225 of those lines are on
80-series materials). **SAP TEAM CONFIRMED, 15-Oct-2026: item category 3 is the
repair-PO filter.** It is kept as configuration anyway, not promoted to a
literal -- VZI can still redefine the convention later, and if they do that
must be one line in ``.env``, not a code change, a review and a redeploy.

Every field here is read from the environment with an ``I8_`` prefix, falling
back to ``.env``, exactly like ``app.core.config.Settings``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.shared.plant_scope import PLANT_NAMES

#: Where usable vs damaged stock can be read from (27-Oct-2026). MCHB is stock
#: per batch, which on this SAP is stock per condition; MBEW is the valuation,
#: split by condition for every 80-series part. See ``stock_condition_source``.
STOCK_CONDITION_SOURCES: tuple[str, ...] = ("MCHB", "MBEW")


class I8Settings(BaseSettings):
    """Initiative 08 rules. Field names map to ``I8_``-prefixed env vars."""

    model_config = SettingsConfigDict(
        env_prefix="I8_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- The complete-flow switch (29-Oct-2026) ---------------------------

    complete_flow_enabled: bool = False
    """Serve the I08 complete flow released 27/28-Oct-2026 (repair PRs, stock
    by condition, the need-date line, declaring in the assistant, R-reference
    links and session jobs, the new exception types, the start dates and the
    notification log). Off by default, so no environment shows features the
    team lead has not approved yet -- production included, when ``dev`` is
    released to ``main``. Turning it on is ``I8_COMPLETE_FLOW_ENABLED=true`` on
    the App Service: a settings change restarts the app, no deploy needed.

    Off, every I08 and reservation-assistant output reads as it did before
    that release. The data layer stays: the snapshot still builds everything,
    and the switch decides only what is served and raised. While it is off,
    :meth:`_complete_flow_gate` reads ``repair_requests_enabled``,
    ``need_date_advice_enabled`` and the two cutover dates as off/blank,
    whatever their own env vars say; the rest is gated where it is served."""

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

    # PSTYP. The filter. SAP team confirmed 15-Oct-2026 -- see the module note.
    repair_item_category: str = "3"

    # BSART. Corroborating attribute ONLY, never a gate: 455 of the 1,225
    # repair lines have no EKKO header in this extract, and inner-joining to
    # get this value silently drops 37% of the register.
    repair_doc_type: str = "ZREP"

    # BSART of a request for quotation. Its lines are left out of the register:
    # an RFQ asks a vendor for a price and orders nothing, and SAP never ticks
    # "Delivery completed" on one, so it would sit there as open for good. One
    # did until 21-Oct-2026 -- 6000016330/10, item category 3, quantity 0 --
    # and the Overview read 105 open lines against 104 under repair. SAP's own
    # open-PO count leaves RFQs out too.
    rfq_doc_type: str = "AN"

    # EKPO.LOEKZ. A line carrying the deleted value is not a repair anybody is
    # waiting on, so it is left out of the register entirely. Before this was
    # read, 44 deleted repair lines sat in the register as open, and most of
    # them as overdue -- a repair that was cancelled in SAP being chased here.
    po_deleted_indicator: str = "L"

    # EKPO.LOEKZ, the blocked value. Left out of the register since
    # 19-Oct-2026, as SAP's own open-PO count leaves it out: on the 15-Oct data
    # SAP's ~106 open repair lines matched ours (104) only without the 68
    # blocked ones. A blocked line can be released again, and it comes back in
    # on the next snapshot when it is.
    po_blocked_indicator: str = "S"

    # EKPO.ELIKZ, the "Delivery completed" flag. A line carrying it is closed,
    # whether or not anything was received against it -- SAP's own rule, and
    # the register's since 19-Oct-2026. Before that the register decided from
    # goods receipts and called 644 lines open that SAP had closed: 640 with
    # nothing received, ticked to close them off on paper.
    delivery_completed_indicator: str = "X"

    # PSTYP of a standard purchase line -- a new unit bought outright. On an
    # 80-series material this is the population the reservation assistant
    # challenges (FRS s4.1: document types ZDOM, AN and WK, item category 0),
    # and what UNJUSTIFIED_ACQUISITION is measured over.
    new_purchase_item_category: str = "0"

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
    #
    # No goods-receipt movements here since 19-Oct-2026: whether a line is
    # still open is SAP's "Delivery completed" flag, not a receipt, and the
    # register no longer reads EKBE.
    dispatch_movement_type: str = "541"
    """Transfer to vendor stock. The unit physically leaving for repair."""

    vendor_special_stock: str = "O"
    """Special-stock indicator for vendor-held stock.

    A 541 posts TWO rows: the vendor side (SOBKZ 'O', SHKZG 'S') and the plant
    side (SOBKZ null, SHKZG 'H'). Picking the vendor side is what stops every
    dispatch quantity being counted twice.
    """

    job_issue_movement_type: str = "261"
    """Goods issue to the job's order: the part leaving the stores against the
    job's reservation (session_jobs.py, 27-Oct-2026)."""

    job_return_movement_type: str = "262"
    """The reversal of that issue -- the part back to stock. Posted on the
    receipt day, not when the unit left the machine (measured 26-Oct-2026)."""

    # --- Repair orders and repair PRs (27-Oct-2026) ----------------------
    #
    # A repair starts as a repair ORDER -- a reservation whose line for the
    # faulty unit carries the movement type below -- and that line names the
    # repair PR raised from it. A repair PR is still open only when no live PO
    # points back to it AND its reservation line is neither deleted nor
    # final-issued AND the PR item itself is not deleted: all of them together
    # give 108 open repair PRs on the 15-Oct extract, not the 122 a looser
    # rule gives. Each value read from SAP is a setting, never a literal.

    repair_reservation_movement_type: str = "543"
    """RESB.BWART of a repair order's line for the faulty unit. 6,271 such
    lines on in-scope 80-series parts on the 15-Oct extract."""

    reservation_deleted_indicator: str = "X"
    """RESB.XLOEK on a deleted reservation line (3,097 of those 6,271)."""

    reservation_final_issue_indicator: str = "X"
    """RESB.KZEAR on a final-issued reservation line (2,660 of those 6,271)."""

    requisition_deleted_indicator: str = "X"
    """EBAN.LOEKZ on a deleted PR item. NOT EKPO's 'L' (po_deleted_indicator):
    the PR table marks deletion 'X' -- 4,820 of the 12,369 in-scope 80-series
    PR lines on the 15-Oct extract, and none of them 'L'."""

    repair_requests_enabled: bool = False
    """Serve the open repair PRs: rows with no PO yet on the register, their
    detail page, and (BE6) the PRs in the assistant's verdict. The snapshot
    always builds them; this only decides whether they are served, so the
    backend can deploy before the frontend that renders a PR row (27-Oct-2026).
    Off by default: a frontend that predates PR rows would show them as POs."""

    # --- Stock by condition (27-Oct-2026) ---------------------------------
    #
    # MARD says how many units a plant holds, not whether they are any use: a
    # part with one DAMAGED unit on the shelf reads as "1 in stock". SAP keeps
    # the condition alongside -- in MCHB's batch field and in MBEW's valuation
    # type -- so the stock can be split into usable and damaged. The MARD total
    # stays the stock on hand; the split is served beside it.

    stock_condition_source: str = "MCHB"
    """Where the split is read from: ``MCHB`` (stock per batch) or ``MBEW``
    (the split valuation's units). MCHB by default, agreed 26-Oct-2026: its
    unrestricted stock matches MARD on all 730 stocked 80-series parts of the
    15-Oct extract. MBEW matches on 717 of them, because it also counts units
    the plant owns but has sent to a vendor (8000004885 at 1500: MBEW DAMAGED
    2, MCHB and MARD 0). Anything else is refused at startup."""

    usable_stock_conditions: str = "NEW,REFURBISHD"
    """Conditions counted as usable, comma-separated. REFURBISHD is SAP's
    10-character spelling, not a typo. 15-Oct extract, in-scope 80-series MCHB
    rows: NEW 1,667, REFURBISHD 975."""

    damaged_stock_conditions: str = "DAMAGED"
    """Conditions counted as damaged (790 MCHB rows on the 15-Oct extract).
    MBEW also values SCRAP, which is neither usable nor damaged and so is
    counted in neither."""

    # --- The assistant's need-date line (27-Oct-2026) ----------------------

    need_date_advice_enabled: bool = False
    """Tell the requester, when an open repair PO is due back on or after
    today, to set the reservation's need date after that date so no new unit
    is bought. Off until SAP confirms that MRP counts an open repair PO as
    stock on the way: if it does not, MRP raises a purchase request whatever
    the need date says, and the advice would be wrong."""

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

    # Days a repair PR may wait for a declaration, and a Repairable declaration
    # for its repair PR, before either gap is raised as an exception
    # (REPAIR_PR_NOT_DECLARED, DECLARED_NO_REPAIR_PR; 27-Oct-2026). Without a
    # grace every PR raised this morning would read as a miss.
    declaration_grace_days: int = 14

    # From when an 80-series job reservation line must carry the assistant's
    # R-reference in its text (MISSING_SESSION_ID, 27-Oct-2026). Blank falls
    # back to the I13 go-live date: before the assistant existed nobody could
    # type one. Set to the release date at deploy.
    session_check_start_date: str = ""

    # The largest quantity one attestation may record. A typo guard, not a
    # business rule: every repair line in the July extract is for quantity 1,
    # and the largest quantity on ANY 80-series PO line is 400. The table is
    # append-only, so a mistyped 1000000000 would otherwise be permanent.
    attestation_max_quantity: int = 1000

    # --- Justification (FR-7 / FR-8) ----------------------------------------

    # How far from a new purchase's raised date a NEW_ACQUISITION justification
    # may sit and still count as covering it. Symmetric, like the attestation
    # window and for the same reason: material + plant + a date is the only key
    # both sides share. And it stays the only one: the purchase is a new-buy PR
    # (since 27-Oct-2026), raised by the MRP and inventory desks with no
    # reservation behind it -- 3 of 6,259 on 80-series parts name one (measured
    # 26-Oct-2026) -- so no session reference can ever reach it. The assistant
    # normally records the reason before the purchase is raised, sometimes after.
    #
    # A PROPOSAL, NOT A CONFIRMED RULE -- it mirrors attestation_window_days so
    # the two controls are measured the same way until VZI says otherwise.
    justification_window_days: int = 30

    # The date the justification control starts applying. Blank means no
    # cutover. Separate from attestation_cutover_date because the two controls
    # go live with different things and may not share a date. Ruled 26-Oct-2026:
    # no VZI cutover dates; each check starts from its feature's release date,
    # so set this at deploy time to the day the purchase-request check is
    # released. A purchase raised before it is labelled "before Spares
    # Automation" rather than accused, exactly as a pre-cutover repair line is.
    justification_cutover_date: str = ""

    # --- Notifications (FR-11) --------------------------------------------

    # Exception types that log a notification the first time an item of that
    # type is found actionable (not raised before its control existed). FR-11
    # asks for notification "through the exception queue and email": the log is
    # written from 27-Oct-2026, the email sender comes later and reads the same
    # rows. Comma separated; a type not named here is still queued, only not
    # notified. Kept to the four checks that ask somebody to act on a line.
    notification_types: str = (
        "OVERDUE_REPAIR,MISSING_ATTESTATION,MISSING_SESSION_ID,UNJUSTIFIED_ACQUISITION"
    )

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

    # Which free-text sources the keyword pass reads, comma separated.
    #
    # FR-2 names three: PO short text (EKPO.TXZ01), PR short text
    # (EBAN.TXZ01) and goods-movement text (MSEG.SGTXT). All three are built
    # and all three are tested. MOVEMENT is NOT in the default, and that is a
    # deliberate decision rather than an omission.
    #
    # The reason: a 541 is by definition a part already on its way to a
    # vendor for repair, so repair words in a movement comment sit mostly on
    # materials that are ALREADY 80-series -- which lands them in
    # `already_coded`, not in the candidate pool. Whether it finds anything
    # that PO and PR text do not is a question about the data, and nobody has
    # measured it yet. Shipping it on by default would spend model calls on
    # the answer before knowing there is one.
    #
    # To measure: set I8_CODING_CANDIDATE_SOURCES=PO,PR,MOVEMENT in one
    # environment and compare `meta.linesBySource` and `meta.materialsFound`
    # against the default. It is a setting, so that costs a restart.
    coding_candidate_sources: str = "PO,PR"

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

    # --- New Item Price (FR-2 coding screen) ------------------------------

    # BWTAR of the NEW condition on a split-valuated material. Every 80-series
    # material is split NEW / REFURBISHD / DAMAGED / SCRAP, and its blank-BWTAR
    # row aggregates those, so for a split material the NEW row is the only
    # new-item price. A material that is not split has one row, BWTAR blank,
    # and that row is the price. The source -- MBEW VERPR -- was confirmed by
    # the SAP team on 16-Oct-2026.
    new_item_valuation_type: str = "NEW"

    # A VERPR at or below this is a placeholder valuation, not a price.
    # Measured on the 15-Oct-2026 export: R1.00 / R0.01 sits on nearly every
    # DAMAGED and SCRAP row and on 18% of NEW rows -- deliberate token values,
    # not data errors. Shown as a price, R1 would make every repair look not
    # worth doing, so the screen shows none and says why.
    new_item_price_token_max: Decimal = Decimal("1.00")

    # Per statement of the MBEW lookup, until its first row. Past it the screen
    # is served without prices (LOOKUP_FAILED) rather than not at all: the
    # frontend gives the whole screen 60 s, and staging plus twins can take 45
    # of them on a busy database. 0 = no limit.
    new_item_price_timeout_seconds: int = 20

    # --- Repair cost (the repair line's detail) ----------------------------

    # An EKPO.NETPR above zero and at or below this is a placeholder price on
    # the PO, not a cost: 16 of the 104 open repair lines carried 1.00 on
    # 29-Oct-2026. The line then shows no cost and says why. The same floor as
    # new_item_price_token_max above, set separately so the two stay
    # independent. In the PO's own currency, like the price it is compared to.
    repair_cost_token_max: Decimal = Decimal("1.00")

    # --- Presentation -----------------------------------------------------

    # Plant code -> display name, comma separated.
    #
    # No plant-name table was delivered, so these are not read from SAP. They
    # are documented facts about this landscape rather than invented labels,
    # and they are the only two plants in scope -- so this map is complete
    # rather than partial, and nothing renders as a bare code.
    #
    # DERIVED from app/shared/plant_scope.py rather than typed out again. Two
    # copies of the same two names drifted once already: I08 said "Black
    # Mountain" while everything else said "Black Mountain Mining", so the same
    # plant read as two different sites depending on which screen you were on.
    # Overriding I8_PLANT_NAMES in the environment still works and still wins.
    plant_names: str = ",".join(f"{code}={name}" for code, name in PLANT_NAMES.items())

    # --- API paging -------------------------------------------------------
    # 781 open repair lines will not render in one response.
    default_page_size: int = 50
    max_page_size: int = 500

    @model_validator(mode="after")
    def _complete_flow_gate(self) -> "I8Settings":
        """With ``complete_flow_enabled`` off, the complete flow's own switches
        and start dates read as off and blank -- the effective settings, so
        every reader (register, declarations, exceptions, assistant, the
        snapshot's rules) agrees without a check of its own.

        ``session_check_start_date`` is left alone on purpose: the snapshot
        build reads it, and a stored snapshot outlives a restart, so blanking
        it here would leave the wrong start date behind after the switch is
        turned on. Its only output, MISSING_SESSION_ID, is not raised while the
        switch is off (exceptions.build_exceptions)."""
        if not self.complete_flow_enabled:
            self.repair_requests_enabled = False
            self.need_date_advice_enabled = False
            self.attestation_cutover_date = ""
            self.justification_cutover_date = ""
        return self

    @field_validator("stock_condition_source")
    @classmethod
    def _known_stock_condition_source(cls, value: str) -> str:
        """Upper-cased, and one of :data:`STOCK_CONDITION_SOURCES` or a
        startup error. Unlike the coding sources, a typo here is not dropped:
        there is no safe fallback that would not quietly change every stock
        figure the assistant quotes."""
        name = value.strip().upper()
        if name not in STOCK_CONDITION_SOURCES:
            raise ValueError(
                f"I8_STOCK_CONDITION_SOURCE must be one of {', '.join(STOCK_CONDITION_SOURCES)}, not {value!r}"
            )
        return name

    @field_validator("damaged_stock_conditions")
    @classmethod
    def _conditions_counted_once(cls, value: str, info: ValidationInfo) -> str:
        """A condition both usable and damaged would be counted twice. A field
        validator, not a model one: a model validator's error quotes the whole
        input, and that includes every other setting in the environment."""
        usable = info.data.get("usable_stock_conditions")
        both = set(_conditions(usable or "")) & set(_conditions(value))
        if both:
            raise ValueError(
                "I8_USABLE_STOCK_CONDITIONS and I8_DAMAGED_STOCK_CONDITIONS both name " + ", ".join(sorted(both))
            )
        return value

    @property
    def usable_stock_condition_list(self) -> tuple[str, ...]:
        """The usable conditions, upper-cased, de-duplicated, in the order
        configured (the assistant's breakdown follows it)."""
        return _conditions(self.usable_stock_conditions)

    @property
    def damaged_stock_condition_list(self) -> tuple[str, ...]:
        """The damaged conditions, upper-cased, de-duplicated, in order."""
        return _conditions(self.damaged_stock_conditions)

    @property
    def coding_candidate_source_list(self) -> tuple[str, ...]:
        """The configured sources, upper-cased and de-duplicated in order.

        An unrecognised name is dropped rather than raising: a typo in one
        environment's setting should narrow the screen visibly -- the response
        reports which sources ran -- not stop the application from starting.
        Dropping every name leaves PO, because a screen that reads nothing is
        never the intended configuration.
        """
        from app.initiatives.i8.coding_candidates import CandidateSource

        known = {s.value for s in CandidateSource}
        seen: list[str] = []
        for raw in self.coding_candidate_sources.split(","):
            name = raw.strip().upper()
            if name in known and name not in seen:
                seen.append(name)
        return tuple(seen) or (CandidateSource.PO.value,)

    @property
    def repair_language_list(self) -> tuple[str, ...]:
        """The repair-language stems, parsed and lower-cased."""
        return tuple(
            word.strip().lower() for word in self.repair_language.split(",") if word.strip()
        )

    @property
    def repair_language_like_patterns(self) -> tuple[str, ...]:
        """The stems as upper-cased ``%stem%`` LIKE patterns, for a database
        prefilter compared against ``upper(text)``.

        LIKE rather than a regex because SQL Server has no ``~*``. Built from
        configuration and passed as BIND PARAMETERS, never interpolated into
        SQL. Each stem is LIKE-escaped, so a keyword containing ``%``, ``_`` or
        ``[`` narrows the search instead of silently becoming a wildcard.
        """
        from app.shared.sql_lists import like_literal

        return tuple(f"%{like_literal(word.upper())}%" for word in self.repair_language_list)

    @property
    def fault_category_list(self) -> tuple[str, ...]:
        """The controlled fault-category list, parsed. Order is preserved
        because the form renders them in it."""
        return tuple(
            c.strip().upper() for c in self.fault_categories.split(",") if c.strip()
        )

    @property
    def notification_type_list(self) -> tuple[str, ...]:
        """The notified exception types, upper-cased and de-duplicated in
        order. Blank turns notification logging off."""
        return _conditions(self.notification_types)

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
    def session_check_start_date_value(self) -> date | None:
        """``session_check_start_date`` as a date, or None meaning "use the
        I13 go-live date". Invalid input raises, like the cutover."""
        if not self.session_check_start_date.strip():
            return None
        return date.fromisoformat(self.session_check_start_date.strip())

    @property
    def justification_cutover_date_value(self) -> date | None:
        """``justification_cutover_date`` as a date, or None meaning "not set".

        Raises on malformed input, for the same reason the attestation cutover
        does: a wrong date silently decides which purchases get accused.
        """
        if not self.justification_cutover_date.strip():
            return None
        return date.fromisoformat(self.justification_cutover_date.strip())

    @property
    def reference_date_value(self) -> date | None:
        """``reference_date`` as a date, or None meaning "use today".

        Invalid input raises here, at startup, rather than producing silently
        wrong aging on every row of every response.
        """
        if not self.reference_date.strip():
            return None
        return date.fromisoformat(self.reference_date.strip())


def _conditions(raw: str) -> tuple[str, ...]:
    """A comma-separated condition list, upper-cased like the SAP values it is
    compared with, blanks and repeats dropped."""
    return tuple(dict.fromkeys(c.strip().upper() for c in raw.split(",") if c.strip()))


@lru_cache
def get_i8_settings() -> I8Settings:
    """The process-wide I08 settings instance."""
    return I8Settings()


def reset_i8_settings_cache() -> None:
    """Drop the cached settings. For tests that vary the rules."""
    get_i8_settings.cache_clear()
