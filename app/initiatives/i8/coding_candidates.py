"""W5.5 -- finding repairable spares that nobody coded as repairable.

What this is, in plain English
------------------------------
A material is repairable only if somebody typed an 80-series number when they
created it. It is a naming convention, not a system flag -- nothing in SAP marks
it.

So when someone gets that wrong, the part becomes invisible to everything I08
builds. It never appears in the universe, never appears in the register, and
when it wears out somebody buys a new one.

This module finds those. It reads the free text people typed on purchase orders
and looks for lines that **talk about repair but are not coded as repairable**.

Two passes, and the first one is not the answer
-----------------------------------------------
The keyword pass is a *filter*, not a verdict. It is cheap, it runs in the
database, and it is deliberately generous. The judgement -- is this a repairable
spare somebody mis-coded, a repair *service* correctly not coded as a material,
or a consumable *used in* repairs -- is language, and that is what the model is
for. The registry routes it accordingly::

    i08_coding_candidate -> capable
    "Language judgement over messy free-text PO lines. A false negative is a
     repairable spare bought new."

What the measurement actually showed, and why it changed the shape
-------------------------------------------------------------------
Measured against the July extract on 15-Sep and re-measured on 21-Sep, when the
scope narrowed to plants 1300 and 1500 (``app/shared/plant_scope.py``). The
task plan's three headline numbers were exactly right for the whole extract; in
scope they are smaller, and the in-scope figures are what this module asserts::

                          in scope   whole extract (pre-21-Sep)
    PO lines with text      80,880    82,718
    non-80-series, repair      296       305
    already 80-series            4         6

But **296 is not the screening population**, and this is the finding that
reshaped the module:

    174 of the 296 (59%) have NO MATERIAL NUMBER AT ALL.

A coding candidate is a material that *should* have been coded 80-series and was
not. A PO line with no material cannot be mis-coded -- there is nothing to
re-code. Those 174 are free-text service purchases, and they are a real and
separate finding (repair spend happening entirely outside the material master),
so they are counted and reported rather than quietly dropped. They are never
candidates.

The proportion barely moved when the scope narrowed -- it was 183 of 305, also
60%. Free-text repair spend is not a quirk of one site.

That leaves 122 lines carrying a material, and those collapse to **41 distinct
materials**, because the same part is bought repeatedly::

    5000075445  18 lines      5000075446  18 lines      5000024401  16 lines

So the screen judges **materials, not lines**. That is both cheaper -- 41 model
calls rather than 296 -- and more correct: the coding is a property of the
material, so judging the same part four times could produce four verdicts and no
way to choose between them. Every line's text is passed in together, which is
also strictly more evidence per call.

A caution the plan did not have: roughly 57 of them are repair *products* --
"REPAIR KIT", "REPAIR CLAMP", "PIPE REPAIR KIT" -- which are consumables that
match the keyword and are correctly not 80-series. They are exactly what the
model is there to reject, and :data:`Verdict.CONSUMABLE_FOR_REPAIR` names that
outcome rather than lumping it in with "no".

Corroboration that needs no model at all
-----------------------------------------
While screening the real data, one candidate turned out to be provable rather
than merely likely::

    RECONDITIONED BRAKE FOOTVALVE EMEM5893
        8000005737   1 line    80-series  -- coded correctly
        5000094118   2 lines   NOT 80-series  -- MIS-CODED

**The identical short text, on the same physical part, coded both ways in the
same extract.** That is not a language judgement anybody can argue with: the
convention demonstrably was applied to this part, and then was not.

:func:`find_twins` looks for exactly that -- an 80-series material carrying text
a candidate also carries. It is deterministic, it costs one query, and where it
fires it turns "a model thinks this reads like a repairable" into "SAP itself
contains the counter-example". A candidate with a twin is the one to put in
front of the SAP team first.

Building it without credentials
-------------------------------
``llm_provider`` defaults to ``stub``, and the stub is deterministic by design.
Everything here is provider-agnostic: the detection, the prompt, the screening
pass, the API and every test. Swapping to the live model is one setting.

The stub cannot return a verdict -- it returns fixed placeholder text -- so a
stub-screened material comes back as :data:`Verdict.UNSCREENED` with the reason
stated. **That is deliberate and it is the honest answer.** Defaulting an
unscreened material to "not a candidate" would silently report zero findings
from a run that never asked a model anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ai import AIError, Completion
from app.core.logging import get_logger
from app.core.prompts import PromptError, complete_with_prompt
from app.initiatives.i8.coding_store import (
    StoredVerdict,
    load_verdicts,
    save_verdict,
    text_fingerprint,
)
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series
from app.core.config import get_settings
from app.initiatives.i8.staged import (
    DIRECT_CODING,
    Sources,
    coding_sources,
    plain_text_total,
    reading,
)
from app.shared.sql_lists import fetch_in_chunks, like_any

logger = get_logger(__name__)

PROMPT_ID = "i08_coding_candidate"


class Verdict(str, Enum):
    """What the screen concluded about one material.

    Four real outcomes and one absence. The absence is a value rather than a
    missing row because "no model has looked at this" and "a model looked and
    said no" are different states, and collapsing them would let a run with no
    credentials report a clean bill of health.
    """

    MISCODED_REPAIRABLE = "MISCODED_REPAIRABLE"
    """A repairable spare that should carry an 80-series number and does not.
    The finding. A false negative here is a repairable spare bought new."""

    REPAIR_SERVICE = "REPAIR_SERVICE"
    """Somebody's labour, not a part. Correctly not a material at all."""

    CONSUMABLE_FOR_REPAIR = "CONSUMABLE_FOR_REPAIR"
    """A part used *in* a repair -- a repair kit, a clamp, a patch. It matches
    the keyword and is correctly not 80-series. Roughly 57 of the screened
    lines look like this, so it is named rather than lumped into "no"."""

    UNCLEAR = "UNCLEAR"
    """The model looked and could not tell. An answer, and a useful one: it
    means a human should read the text."""

    UNSCREENED = "UNSCREENED"
    """No model answered -- no credentials, the stub provider, or a failure.
    Never treated as "not a candidate"."""


#: The verdicts a human should act on.
ACTIONABLE: frozenset[Verdict] = frozenset({Verdict.MISCODED_REPAIRABLE, Verdict.UNCLEAR})

# Rank, not an IntEnum: confidence isn't ordinal by nature, only by this one
# caller's need to compare it against a configured threshold. Same idiom as
# app.core.criticality.SEVERITY_ORDER.
_CONFIDENCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def meets_confidence_threshold(confidence: str, threshold: str) -> bool:
    """Does this candidate clear the configured confidence bar?

    Unscreened candidates carry an empty ``confidence`` and never meet any
    threshold, however low -- the same rule that keeps :data:`Verdict.UNSCREENED`
    from ever being read as a real judgement. An unrecognised ``threshold``
    (a config typo) is treated as the most permissive, "low" -- a filter that
    fails open rather than silently hiding every candidate.
    """
    return _CONFIDENCE_RANK.get(confidence, -1) >= _CONFIDENCE_RANK.get(threshold, 0)


class CandidateSource(str, Enum):
    """Which free-text field a line's words came from.

    FR-2 names three, and they are not interchangeable evidence. A PO line is
    money already committed. A PR line is a request that may never become one,
    and is the earlier -- so more useful -- signal. A movement comment is
    neither: it is a note somebody typed while moving stock.

    Carried on every line rather than inferred, because "4500000001/10" and
    "0010000123/0001" look alike and mean different things, and a reader
    checking a verdict needs to know which document to open.
    """

    PO = "PO"
    """EKPO.TXZ01 -- purchase order item short text."""

    PR = "PR"
    """EBAN.TXZ01 -- purchase requisition item short text."""

    MOVEMENT = "MOVEMENT"
    """MSEG.SGTXT -- goods movement item text."""


@dataclass(frozen=True)
class CandidateLine:
    """One document line whose free text mentions repair."""

    source: str
    """Which of :class:`CandidateSource` this came from."""

    document: str
    """EBELN for a PO, BANFN for a PR, MBLNR for a movement."""

    document_item: str
    """EBELP, BNFPO or ZEILE. Deliberately NOT called ``item``: the three
    number differently, and a bare ``item`` reads as a PO item everywhere
    else in this package."""

    material_id: str | None
    """None on 174 of the 296 matching PO lines -- a free-text service
    purchase. A movement always carries one; a requisition need not."""

    plant: str | None
    short_text: str
    matched: tuple[str, ...]
    """Which configured keywords this text hit. Carried so a reader can see why
    the line was picked up without re-running the regex in their head."""

    raised_at: date | None
    item_category: str | None
    """PSTYP on a PO or PR line. Always None on a movement, which has no item
    category at all -- an absence, not an unknown."""

    deleted: bool = False
    """LOEKZ is set on this line. Only ever true on :data:`CandidateSource.PR`:
    EKPO's deleted lines are already out of scope upstream, and a goods
    movement is reversed rather than deleted."""

    became: tuple[str, str] | None = None
    """For a PR line, the ``(EBELN, EBELP)`` it was converted into.

    Only ever set on :data:`CandidateSource.PR`. It exists so
    :func:`drop_converted_requisitions` can recognise the duplicate without a
    second query, and so a dropped line can be explained rather than merely
    absent.
    """


@dataclass(frozen=True)
class CodingCandidate:
    """One material, every repair-language line it appears on, and the verdict."""

    material_id: str
    lines: tuple[CandidateLine, ...]
    verdict: str
    confidence: str
    """``high`` / ``medium`` / ``low``, as the model reported it. Empty when
    unscreened."""

    reason: str
    """Why, in the model's words. The thing a human checks the call against."""

    in_repairable_universe: bool
    """Cross-checked against W5.1. A material already in the universe is not a
    coding candidate however its text reads -- it is already coded correctly."""

    twins: tuple[tuple[str, str], ...] = ()
    """``(80-series material, the shared text)`` pairs proving this exact part
    IS coded correctly elsewhere. See :func:`find_twins`.

    Where this is non-empty the finding stops being a judgement and becomes an
    observation: the same words, on the same part, coded both ways in one
    extract."""

    model: str = ""
    provider: str = ""
    """WHO answered -- ``stub``, ``foundry``, ``openai``. Deliberately separate
    from ``model``: ``model`` is the name the registry ROUTED to, and the stub
    echoes that name straight back, so a stub run reports ``gpt-4o`` as its
    model. Only the provider says whether anything was really judged."""

    prompt_version: int | None = None
    screened_at: datetime | None = None
    raw_response: str = ""
    """Kept when parsing failed, so an unparseable answer can be diagnosed
    rather than silently discarded."""

    @property
    def plants(self) -> tuple[str, ...]:
        return tuple(sorted({line.plant for line in self.lines if line.plant}))

    @property
    def texts(self) -> tuple[str, ...]:
        """The distinct free text this material was judged on."""
        seen: list[str] = []
        for line in self.lines:
            if line.short_text not in seen:
                seen.append(line.short_text)
        return tuple(seen)

    @property
    def is_actionable(self) -> bool:
        return Verdict(self.verdict) in ACTIONABLE

    @property
    def is_corroborated(self) -> bool:
        """True when SAP contains the counter-example. The strongest evidence
        this screen can produce, and it owes nothing to the model."""
        return bool(self.twins)


@dataclass(frozen=True)
class ScreenStats:
    """Counts, each with the thing it counts stated. Never a bare number."""

    sources: tuple[str, ...] = ()
    """Which of :class:`CandidateSource` this run actually read.

    First field, and reported on every response, because every count below is
    relative to it: "41 materials found" means something different when
    requisitions were read and when they were not. A run cannot be compared
    against another without it."""

    lines_with_text: int = 0
    """Lines carrying ANY free text, across the sources that ran. The
    denominator -- how much text was searched."""

    staged_rows_kept: dict[str, int] = field(default_factory=dict)
    """Rows each staged copy KEPT, per view. Empty when the screen read the
    views directly.

    Reported rather than only logged because the whole approach rests on this
    being a small fraction of :attr:`staged_rows_scanned`. If a keyword list is
    ever widened until the narrowing stops narrowing, this is the pair of
    numbers that shows it -- before somebody has to rediscover why the screen
    got slow again."""

    staged_rows_scanned: dict[str, int] = field(default_factory=dict)
    """Rows each staged copy was narrowed FROM, per view -- the in-scope size
    of the normalised table."""


    lines_with_text_by_source: dict[str, int] = field(default_factory=dict)
    lines_with_repair_language: int = 0
    lines_by_source: dict[str, int] = field(default_factory=dict)
    """Repair-language hits per source. The number to read when deciding
    whether a source earns its keep -- see I8_CODING_CANDIDATE_SOURCES."""

    deleted_requisitions_dropped: int = 0
    """PR lines dropped because LOEKZ is set. A deleted requisition is not a
    request for anything, so its text is not evidence of anything."""

    converted_requisitions_dropped: int = 0
    """PR lines dropped because the PO they became is in this same run.

    One request, counted once. Reported rather than silent: a reader comparing
    `lines_with_repair_language` against the per-source numbers would otherwise
    find them not adding up."""

    lines_already_eighty_series: int = 0
    """Repair language on a material that IS coded repairable. Not a candidate
    -- the convention worked. Measured at 6."""

    lines_without_material: int = 0
    """Free-text service purchases. Cannot be mis-coded because there is nothing
    to re-code. Measured at 174, which is 59% of the matches."""

    lines_screened: int = 0
    materials_screened: int = 0
    materials_found: int = 0
    """How many materials the keyword pass produced. Differs from
    ``materials_screened`` only when a limit truncated the run, and the two are
    reported separately so a truncated screen cannot read as a complete one."""

    verdicts_reused: int = 0
    """How many verdicts came from the store rather than from a model call.

    The difference between a four-minute run and a four-second one, and worth
    reporting for that reason alone: a reader who sees 41 materials screened in
    three seconds should be able to see WHY without guessing that something was
    skipped."""

    corroborated: int = 0
    """How many candidates have an 80-series twin -- the ones that are provable
    from the data rather than argued from language."""

    by_verdict: dict[str, int] = field(default_factory=dict)
    keywords: tuple[str, ...] = ()
    provider: str = ""
    """WHO answered: ``stub``, ``foundry`` or ``openai``. ``stub`` means nothing
    was really judged, and every verdict will be UNSCREENED."""

    model: str = ""
    """The deployment the registry routed to. NOT evidence that a real model
    ran -- the stub echoes the routed name back, so this reads ``gpt-4o`` on a
    stub run too. Read ``provider`` for that."""

    @property
    def was_truncated(self) -> bool:
        return self.materials_found > self.materials_screened


# Every PO line whose free text mentions repair.
#
# The keywords are BIND PARAMETERS built from configuration, never
# interpolated -- so there is still no repair vocabulary written into a query.
# One upper-cased LIKE per keyword, ORed into {keyword_match}: SQL Server has no
# case-insensitive regex match (Postgres's ``~*``), and upper() on both sides
# keeps the match case-insensitive whatever the column's collation.
#
# Note what is NOT here: any exclusion of 80-series materials. The prefilter
# contract (material_number.series_like_patterns) says the database may only
# ever be LOOSER than the Python predicate, and `NOT LIKE '80%'` would be
# stricter -- it would drop a material like '80' that the real predicate
# rejects anyway, but also anything the predicate would have kept. The set is
# ~300 rows, so it is filtered in Python where the one true test lives.
# One per source. The shape is identical -- "every line of this document type
# whose free text matches a configured keyword" -- but the column names, the
# date that means "raised", and whether an item category exists at all are
# not, so each carries its own mapper rather than being forced into a UNION
# with null padding. A UNION would also have to agree on types across three
# views and two dialects, which is a lot of casting to save one round trip
# over a few hundred rows.
#
# Note what is NOT in any of them: no 80-series exclusion (partition() owns
# that), no deletion filter and no conversion filter. Those are judgements,
# they are applied in Python where they can be unit-tested and counted, and
# the counts are reported. See 24_v_eban.sql.


@dataclass(frozen=True)
class _SourceQuery:
    """How to read one free-text source."""

    source: str
    text_column: str
    """The qualified column the keyword patterns are matched against."""

    candidates: str
    """SQL with a ``{keyword_match}`` placeholder."""

    text_total: str
    """How many lines of this source carry any text at all -- the denominator."""

    def text_of(self, row) -> str:
        """The free text this source screens, from one row."""
        raise NotImplementedError

    def to_line(self, row, matched: tuple[str, ...]) -> "CandidateLine":
        raise NotImplementedError


@dataclass(frozen=True)
class _PurchaseOrderSource(_SourceQuery):
    def text_of(self, row):
        return row["txz01"]

    def to_line(self, row, matched):
        return CandidateLine(
            source=CandidateSource.PO.value,
            document=row["ebeln"],
            document_item=row["ebelp"],
            material_id=row["matnr"],
            plant=row["werks"],
            short_text=row["txz01"],
            matched=matched,
            raised_at=row["erdat"],
            item_category=row["pstyp"],
        )


@dataclass(frozen=True)
class _RequisitionSource(_SourceQuery):
    def text_of(self, row):
        return row["txz01"]

    def to_line(self, row, matched):
        po = row["ebeln"]
        item = row["ebelp"]
        return CandidateLine(
            source=CandidateSource.PR.value,
            document=row["banfn"],
            document_item=row["bnfpo"],
            material_id=row["matnr"],
            plant=row["werks"],
            short_text=row["txz01"],
            matched=matched,
            # BADAT, the requisition date. The PR equivalent of EKPO.ERDAT:
            # when somebody asked, rather than when a buyer acted.
            raised_at=row["badat"],
            item_category=row["pstyp"],
            deleted=bool((row["loekz"] or "").strip()),
            became=(po, item) if po and item else None,
        )


@dataclass(frozen=True)
class _MovementSource(_SourceQuery):
    def text_of(self, row):
        return row["sgtxt"]

    def to_line(self, row, matched):
        return CandidateLine(
            source=CandidateSource.MOVEMENT.value,
            document=row["mblnr"],
            document_item=row["zeile"],
            material_id=row["matnr"],
            plant=row["werks"],
            short_text=row["sgtxt"],
            matched=matched,
            # BUDAT. Not "raised" in the sense a PO line is -- a movement is
            # not a request for anything -- but it is when the words were
            # written, which is what the column means to a reader.
            raised_at=row["budat"],
            # A goods movement has no item category. None here is an absence,
            # not an unknown.
            item_category=None,
        )


_SOURCES: dict[str, _SourceQuery] = {
    CandidateSource.PO.value: _PurchaseOrderSource(
        source=CandidateSource.PO.value,
        text_column="upper(p.txz01)",
        candidates="""
select
    p.ebeln, p.ebelp, p.matnr, p.werks, p.txz01, p.erdat, p.pstyp
from v_ekpo p
where coalesce(p.txz01, '') <> ''
  and {keyword_match}
order by p.ebeln, p.ebelp
""",
        text_total="select count(*) from v_ekpo where coalesce(txz01, '') <> ''",
    ),
    CandidateSource.PR.value: _RequisitionSource(
        source=CandidateSource.PR.value,
        text_column="upper(r.txz01)",
        candidates="""
select
    r.banfn, r.bnfpo, r.matnr, r.werks, r.txz01, r.badat, r.pstyp,
    r.loekz, r.ebeln, r.ebelp
from v_eban r
where coalesce(r.txz01, '') <> ''
  and {keyword_match}
order by r.banfn, r.bnfpo
""",
        text_total="select count(*) from v_eban where coalesce(txz01, '') <> ''",
    ),
    CandidateSource.MOVEMENT.value: _MovementSource(
        source=CandidateSource.MOVEMENT.value,
        text_column="upper(m.sgtxt)",
        candidates="""
select
    m.mblnr, m.zeile, m.matnr, m.werks, m.sgtxt, m.budat
from v_mseg m
where coalesce(m.sgtxt, '') <> ''
  and {keyword_match}
order by m.mblnr, m.zeile
""",
        text_total="select count(*) from v_mseg where coalesce(sgtxt, '') <> ''",
    ),
}


def matched_keywords(short_text: str, cfg: I8Settings) -> tuple[str, ...]:
    """Which configured keywords this text hits. Case-insensitive substrings."""
    lowered = (short_text or "").lower()
    return tuple(word for word in cfg.repair_language_list if word in lowered)


def fetch_repair_language_lines(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    sources: Sequence[str] | None = None,
    staged: Sources | None = None,
) -> list[CandidateLine]:
    """Every line whose free text mentions repair, coded or not.

    The keyword pass, and nothing more. It does not decide anything: the
    80-series test, the deletion rule, the conversion rule and the model all
    run over what this returns.

    ``sources`` overrides ``I8_CODING_CANDIDATE_SOURCES``. Tests that assert a
    measured figure pass it explicitly, because every one of those figures was
    measured against PO text alone and adding a source silently would turn a
    documented measurement into a number nobody has ever checked.

    ``staged`` points the same SQL at narrowed copies of the views -- see
    :mod:`app.initiatives.i8.staged`. The SQL is unchanged; only the table it
    reads is. ``None`` reads the views directly, which is what every test
    without a SQL Server does.
    """
    cfg = cfg or get_i8_settings()
    wanted = tuple(sources) if sources is not None else cfg.coding_candidate_source_list

    lines: list[CandidateLine] = []
    for name in wanted:
        spec = _SOURCES.get(name)
        if spec is None:
            continue
        keyword_match, params = like_any(
            spec.text_column, "keyword", cfg.repair_language_like_patterns
        )
        rows = (
            db.execute(
                text(reading(spec.candidates.replace("{keyword_match}", keyword_match), staged)),
                params,
            )
            .mappings()
            .all()
        )
        lines.extend(
            spec.to_line(row, matched_keywords(spec.text_of(row), cfg)) for row in rows
        )
    return lines


def fetch_text_totals(
    db: Session, sources: Sequence[str], staged: Sources | None = None
) -> dict[str, int]:
    """How many lines of each source carry any free text -- the denominator.

    Separate from the keyword pass because it is the thing the hit count is a
    fraction OF, and a hit count served without it ("296 lines mention
    repair") is a number with no scale.

    **Never reads the staged candidate copies.** Those hold only the lines
    whose text mentions repair, so counting them would make the denominator
    equal the numerator and every source read as 100%. When staging is on this
    counts the normalised table directly, with the view's own scope predicate
    written inline -- a plain set-wise scan, not a per-row function call.
    """
    totals: dict[str, int] = {}
    for name in sources:
        spec = _SOURCES.get(name)
        if spec is None:
            continue
        plain = plain_text_total(name) if staged is not None else None
        totals[name] = int(db.execute(text(plain or spec.text_total)).scalar() or 0)
    return totals


def drop_deleted_requisitions(
    lines: Sequence[CandidateLine],
) -> tuple[list[CandidateLine], int]:
    """Remove PR lines flagged for deletion. Returns what is left, and how many went.

    A deleted requisition is not a request for anything, so its text is not
    evidence that somebody wants a repairable part. The register already
    applies this rule to PO lines; applying a different one here would mean
    the same material read as a candidate on one screen and not the other.
    """
    kept = [line for line in lines if not line.deleted]
    return kept, len(lines) - len(kept)


def drop_converted_requisitions(
    lines: Sequence[CandidateLine],
) -> tuple[list[CandidateLine], int]:
    """Remove PR lines whose PO is already in this run. One request, counted once.

    A requisition that has been converted carries the ``(EBELN, EBELP)`` it
    became. When that PO line is also in the keyword hits -- which it will be
    whenever the buyer kept the requester's wording, and they usually do --
    the same request is present twice. Counting it twice inflates
    ``lines_screened`` and shows a reader two rows for one event.

    The PO is kept rather than the PR, deliberately: it is the document that
    exists in SAP now, it is what a cataloguer would open, and it carries the
    item category the PR's copy may not.

    A PR pointing at a PO that did NOT match the keywords is kept -- the buyer
    rewrote the text, and the requester's original wording is then the only
    place the repair language survives.
    """
    po_keys = {
        (line.document, line.document_item)
        for line in lines
        if line.source == CandidateSource.PO.value
    }
    kept = [
        line
        for line in lines
        if not (
            line.source == CandidateSource.PR.value
            and line.became is not None
            and line.became in po_keys
        )
    ]
    return kept, len(lines) - len(kept)


@dataclass(frozen=True)
class Screenable:
    """What the keyword pass produced, already split by what can be judged."""

    by_material: dict[str, list[CandidateLine]]
    """Material -> its repair-language lines. The screening population."""

    already_coded: list[CandidateLine]
    """Repair language on a material that IS 80-series. The convention worked."""

    without_material: list[CandidateLine]
    """No material number. A service purchase, and a finding in its own right --
    but never a coding candidate, because there is nothing to re-code."""

    all_lines: list[CandidateLine]


def partition(lines, cfg: I8Settings | None = None) -> Screenable:
    """Split the keyword hits into the three groups that mean different things.

    This is where the population shrinks from 296 to 41, and the reason is
    stated in the module docstring: coding is a property of a MATERIAL, and 60%
    of the hits have no material at all.
    """
    cfg = cfg or get_i8_settings()

    by_material: dict[str, list[CandidateLine]] = {}
    already_coded: list[CandidateLine] = []
    without_material: list[CandidateLine] = []

    for line in lines:
        if line.material_id is None:
            without_material.append(line)
            continue
        # THE gate, the same predicate the universe uses. A material that is
        # already 80-series is coded correctly, whatever its text says.
        if is_eighty_series(line.material_id, cfg):
            already_coded.append(line)
            continue
        by_material.setdefault(line.material_id, []).append(line)

    return Screenable(
        by_material=by_material,
        already_coded=already_coded,
        without_material=without_material,
        all_lines=list(lines),
    )


# An 80-series material carrying the SAME short text as a non-80-series one.
#
# The text is compared upper-cased and whitespace-collapsed, because these are
# hand-typed catalogue lines and "REFURBISHED  LINCOLN LN-25" and "Refurbished
# Lincoln LN-25" are the same description. It is NOT fuzzy beyond that: a
# near-match would turn hard evidence back into a judgement, which is the one
# thing this check exists to avoid.
#
# The normalising and the join happen in Python, over the texts of just the
# materials involved: collapsing whitespace needs a regex, which SQL Server
# does not reliably have. The rows are few -- one per PO line of a coded or
# suspect material.
_TWIN_TEXTS_SQL = """
select matnr, txz01
from v_ekpo
where matnr in :materials
  and coalesce(txz01, '') <> ''
"""


def normalise_text(value: str) -> str:
    """Upper-cased, trimmed, every whitespace run collapsed to one space."""
    return " ".join(value.split()).upper()


def find_twins(
    db: Session, materials, cfg: I8Settings | None = None, staged: Sources | None = None
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Suspect material -> the 80-series materials sharing its exact text.

    The corroboration described in the module docstring, and the only part of
    this screen that produces evidence rather than an opinion. One pass for
    every candidate at once (chunked only to stay under SQL Server's
    parameter limit).

    The 80-series side is decided by :func:`is_eighty_series` in Python, not in
    SQL -- same prefilter contract as everywhere else in I08.
    """
    cfg = cfg or get_i8_settings()
    suspects = sorted({m for m in materials if m})
    if not suspects:
        return {}

    # Every material that IS coded repairable, by the one true predicate.
    coded = sorted(
        {
            row
            for row in db.execute(
                text(reading("select distinct matnr from v_ekpo where matnr is not null", staged))
            ).scalars()
            if is_eighty_series(row, cfg)
        }
    )
    if not coded:
        return {}

    coded_set, suspect_set = set(coded), set(suspects)

    # normalised text -> {material: its alphabetically first raw text}. The
    # first raw text is the sample shown as the shared text, as min() gave.
    by_text: dict[str, dict[str, str]] = {}

    def remember(material: str, raw: str) -> None:
        if not raw:
            return
        materials_with_text = by_text.setdefault(normalise_text(raw), {})
        current = materials_with_text.get(material)
        if current is None or raw < current:
            materials_with_text[material] = raw

    # The CODED side comes from v_ekpo: the convention being applied correctly
    # is a fact about the purchase order catalogue.
    for row in fetch_in_chunks(db, reading(_TWIN_TEXTS_SQL, staged), "materials", sorted(coded_set)):
        remember(row["matnr"], row["txz01"])

    # The SUSPECT side comes from the lines already in hand, whatever source
    # they came from -- NOT from a second query over v_ekpo. A candidate whose
    # repair language is on a requisition or a movement comment may have no PO
    # line at all, and re-reading v_ekpo for it would find no text, hence no
    # twin, for precisely the candidates the extra sources contributed.
    #
    # A caller may also pass bare material numbers with no lines (the rules
    # tests do). There is nothing in hand for those, so their text is read from
    # v_ekpo as before -- the original behaviour, kept rather than made an
    # error, because it is correct whenever the suspect DOES have a PO line.
    lines_by_material = materials if hasattr(materials, "items") else {}
    for material in suspects:
        for line in lines_by_material.get(material, ()):
            remember(material, line.short_text)

    textless = [m for m in suspects if not lines_by_material.get(m)]
    if textless:
        for row in fetch_in_chunks(db, _TWIN_TEXTS_SQL, "materials", sorted(textless)):
            remember(row["matnr"], row["txz01"])

    found: dict[str, list[tuple[str, str]]] = {}
    for materials_with_text in by_text.values():
        coded_here = sorted(m for m in materials_with_text if m in coded_set)
        for suspect in sorted(m for m in materials_with_text if m in suspect_set):
            for coded_material in coded_here:
                pair = (coded_material, materials_with_text[coded_material])
                if pair not in found.get(suspect, []):
                    found.setdefault(suspect, []).append(pair)

    if found:
        logger.info(
            "I08 coding candidates: %d of %d suspects have an 80-series twin "
            "carrying identical text -- %s",
            len(found),
            len(suspects),
            ", ".join(f"{k}~{v[0][0]}" for k, v in sorted(found.items())),
        )
    return {k: tuple(v) for k, v in found.items()}


# --- The model pass --------------------------------------------------------

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


def parse_verdict(response: str) -> tuple[Verdict, str, str] | None:
    """A model answer as ``(verdict, confidence, reason)``, or None.

    Returns None rather than a default when the answer cannot be read. The stub
    provider returns fixed placeholder prose and lands here every time, which is
    exactly the case that must NOT quietly become "not a candidate".

    Lenient about what surrounds the JSON -- models add prose either side -- and
    strict about what is inside it.
    """
    match = _JSON_OBJECT.search(response or "")
    if match is None:
        return None

    try:
        payload = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    raw_verdict = str(payload.get("verdict", "")).strip().upper()
    try:
        verdict = Verdict(raw_verdict)
    except ValueError:
        return None
    if verdict is Verdict.UNSCREENED:
        # UNSCREENED is ours to assign, never the model's to claim.
        return None

    confidence = str(payload.get("confidence", "")).strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "low"

    reason = str(payload.get("reason", "")).strip()
    return verdict, confidence, reason


def _unscreened(
    material_id: str,
    lines,
    *,
    in_universe: bool,
    twins: tuple[tuple[str, str], ...] = (),
    reason: str,
    raw: str = "",
) -> CodingCandidate:
    """A candidate nobody has judged, and why. Never a default of "no"."""
    return CodingCandidate(
        material_id=material_id,
        lines=tuple(lines),
        verdict=Verdict.UNSCREENED.value,
        confidence="",
        reason=reason,
        in_repairable_universe=in_universe,
        twins=twins,
        raw_response=raw,
    )


def screen_material(
    material_id: str,
    lines,
    *,
    in_universe: bool,
    twins: tuple[tuple[str, str], ...] = (),
    cfg: I8Settings | None = None,
) -> CodingCandidate:
    """Ask the model about one material. One call, all of its text.

    Every failure mode lands on :data:`Verdict.UNSCREENED` with the reason
    recorded, because the alternative -- an exception that stops the run, or a
    default of "not a candidate" -- either loses the other 40 materials or
    reports a clean answer nobody computed.
    """
    cfg = cfg or get_i8_settings()
    ordered = tuple(lines)

    texts: list[str] = []
    for line in ordered:
        if line.short_text not in texts:
            texts.append(line.short_text)

    plants = sorted({line.plant for line in ordered if line.plant})

    def unscreened(reason: str, raw: str = "") -> CodingCandidate:
        return _unscreened(
            material_id,
            ordered,
            in_universe=in_universe,
            twins=twins,
            reason=reason,
            raw=raw,
        )

    completion: Completion
    try:
        completion = complete_with_prompt(
            PROMPT_ID,
            material=material_id,
            plants=", ".join(plants) if plants else "not recorded",
            line_count=str(len(ordered)),
            po_text="\n".join(f"- {t}" for t in texts),
        )
    except PromptError as exc:
        return unscreened(f"prompt unavailable: {exc}")
    except AIError as exc:
        # Configuration or provider failure. Reported per material rather than
        # raised, so one bad call does not lose the rest of the screen.
        return unscreened(f"model unavailable: {exc}")

    parsed = parse_verdict(completion.text)
    if parsed is None:
        return CodingCandidate(
            material_id=material_id,
            lines=ordered,
            verdict=Verdict.UNSCREENED.value,
            confidence="",
            reason=(
                f"the {completion.provider or 'configured'} provider did not return "
                "a readable verdict. The stub provider never does -- it returns "
                "fixed placeholder text by design -- so this is the expected "
                "result with no credentials configured."
            ),
            in_repairable_universe=in_universe,
            twins=twins,
            model=completion.model,
            provider=completion.provider,
            prompt_version=completion.prompt_version,
            screened_at=datetime.now(timezone.utc),
            raw_response=completion.text,
        )

    verdict, confidence, reason = parsed
    return CodingCandidate(
        material_id=material_id,
        lines=ordered,
        verdict=verdict.value,
        confidence=confidence,
        reason=reason,
        in_repairable_universe=in_universe,
        twins=twins,
        model=completion.model,
        provider=completion.provider,
        prompt_version=completion.prompt_version,
        screened_at=datetime.now(timezone.utc),
    )


# --- Ranking ---------------------------------------------------------------
#
# The screen used to serve candidates in material-number order, which puts the
# strongest finding and the weakest one wherever their numbers happen to fall.
# FR-2 asks for a ranked list.
#
# Note what is NOT reordered: `materials = sorted(...)` inside screen() below.
# That sort decides WHICH materials a `limit` screens, so it has to stay
# deterministic and independent of any verdict -- verdicts do not exist yet at
# that point. Ranking is therefore a presentation order applied after the
# judging, never a change to what gets judged.

#: Most worth a human's time first. UNSCREENED last, because it is the absence
#: of a judgement rather than a weak one -- but it is still SERVED, never
#: dropped: a screen that quietly hides what it did not judge is the failure
#: this module is built around.
_VERDICT_RANK: dict[str, int] = {
    Verdict.MISCODED_REPAIRABLE.value: 0,
    Verdict.UNCLEAR.value: 1,
    Verdict.CONSUMABLE_FOR_REPAIR.value: 2,
    Verdict.REPAIR_SERVICE.value: 3,
    Verdict.UNSCREENED.value: 4,
}


def rank_candidates(candidates: Sequence[CodingCandidate]) -> list[CodingCandidate]:
    """Order candidates by how much they deserve a cataloguer's attention.

    1. **Corroborated first.** A twin is SAP contradicting itself -- the same
       description on the same part, coded both ways -- and it owes nothing to
       a model. It outranks any verdict, including a high-confidence one,
       because it is evidence rather than opinion.
    2. **Verdict**, most actionable first (:data:`_VERDICT_RANK`).
    3. **Confidence**, high before low. Empty confidence sorts last, the same
       rule :func:`meets_confidence_threshold` applies: unscreened is not a
       weak judgement, it is no judgement.
    4. **Evidence volume** -- more repair-language lines, more to go on.
    5. **Material number**, so the order is stable between two identical runs
       and a reader can diff one against the next.

    Rules 2 and 3 do nothing on a run with no model, where every verdict is
    UNSCREENED and every confidence empty. That is deliberate, and it is why
    rules 1 and 4 exist: the deterministic half of the screen still produces an
    order worth reading.
    """
    return sorted(
        candidates,
        key=lambda c: (
            0 if c.is_corroborated else 1,
            _VERDICT_RANK.get(c.verdict, len(_VERDICT_RANK)),
            -_CONFIDENCE_RANK.get(c.confidence, -1),
            -len(c.lines),
            c.material_id,
        ),
    )


# --- The whole screen ------------------------------------------------------


def _from_stored(
    material_id: str,
    lines: Sequence[CandidateLine],
    stored: StoredVerdict,
    *,
    in_universe: bool,
    twins: tuple[tuple[str, str], ...],
) -> CodingCandidate:
    """Rebuild a candidate from a verdict already paid for.

    The twins and the universe cross-check are recomputed every run rather than
    read back: both are deterministic, both are cheap, and both are facts about
    the extract rather than about the model's opinion. Only the judgement is
    cached.
    """
    return CodingCandidate(
        material_id=material_id,
        lines=tuple(lines),
        verdict=stored.verdict,
        confidence=stored.confidence,
        reason=stored.reason,
        in_repairable_universe=in_universe,
        twins=twins,
        model=stored.model,
        provider=stored.provider,
        prompt_version=stored.prompt_version,
        screened_at=stored.screened_at,
    )


def screen(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    limit: int | None = None,
    universe_materials: set[str] | None = None,
    use_model: bool = True,
    sources: Sequence[str] | None = None,
    reuse_stored: bool = True,
    staged: bool | None = None,
) -> tuple[list[CodingCandidate], ScreenStats]:
    """The whole screen: keyword pass, exclusions, split, then the model.

    ``use_model=False`` runs everything EXCEPT the model: the keyword pass, the
    exclusions, the split, and the twin corroboration -- all of which are
    deterministic, need no credentials and take under a second. Every material
    comes back UNSCREENED, which is the honest description of what happened.

    That split exists because the model pass is slow enough to matter. Measured
    on 15-Sep against live gpt-4o: **41 materials, 246 seconds**, roughly six
    seconds each and strictly sequential. That is fine for a batch and far too
    slow for a page load, so the API defaults to the fast half and asks for the
    model explicitly.

    ``reuse_stored`` serves a verdict already paid for when the material's text
    has not changed since (:mod:`app.initiatives.i8.coding_store`). It is what
    makes the run after a reload cost seconds rather than four minutes. Pass
    ``False`` to force every material back to the model -- what you want after
    a prompt or deployment change, which no automatic rule can detect.

    ``sources`` overrides ``I8_CODING_CANDIDATE_SOURCES``; see
    :func:`fetch_repair_language_lines`.

    ``staged`` (default: ``I8_SNAPSHOT_STAGED``, on) reads narrowed copies of
    the views instead of the views themselves -- the same treatment the
    snapshot build gets, and for the same reason. Off reads the views
    directly, as this screen did until 13-Oct-2026; the parity test builds a
    seeded tenant both ways and compares every candidate.

    ``universe_materials`` is the repairable universe, passed in rather than
    rebuilt -- the caller already holds it in the cached snapshot and building
    it here would add five seconds to every screen. It is the cross-check the
    task plan asks for: a candidate must NOT already be in the universe, which
    is what ``GET /api/i8/universe/{materialId}`` returning 404 means.

    That check is expected to be tautological today, because
    :func:`partition` has already removed every 80-series material and the
    universe is exactly the 80-series materials. It is kept anyway, and
    reported per candidate, precisely so that if the two rules ever disagree it
    shows up as a visible flag rather than as a quietly wrong candidate list.

    ``limit`` caps how many materials are screened. **When it bites, it is
    logged and reported** -- a silent cap reads as "we checked everything" when
    it did not.
    """
    cfg = cfg or get_i8_settings()
    wanted = tuple(sources) if sources is not None else cfg.coding_candidate_source_list

    # Every read this screen makes happens in here, over narrowed copies of
    # the views -- see app/initiatives/i8/staged.py. It closes before the model
    # loop below, which stores verdicts and therefore commits: a commit can
    # hand the session a different connection, and the temp tables live on the
    # one that made them.
    enabled = get_settings().i8_snapshot_staged if staged is None else staged
    with coding_sources(db, cfg, wanted=wanted, enabled=enabled) as src:
        raw = fetch_repair_language_lines(db, cfg, sources=wanted, staged=src.candidates)
        # Counted before the exclusions, so each per-source number describes
        # what that source actually carries rather than what survived our rules.
        lines_by_source: dict[str, int] = {}
        for line in raw:
            lines_by_source[line.source] = lines_by_source.get(line.source, 0) + 1

        lines, deleted_dropped = drop_deleted_requisitions(raw)
        lines, converted_dropped = drop_converted_requisitions(lines)

        groups = partition(lines, cfg)
        known_universe = universe_materials or set()
        # Computed BEFORE the model runs, for all candidates at once. It is
        # deterministic, so it is worth having even when no model answers: a
        # stub-provider run still reports which suspects SAP itself contradicts.
        twins = find_twins(db, groups.by_material, cfg, staged=src.twins)

        # Moved up from after the model loop, where it used to sit: it is a
        # plain count and it has to be taken while the staging is open.
        # Passing the staged sources switches it to the plain-predicate count
        # over n_*; without staging it reads the view, as it always did.
        text_totals = fetch_text_totals(
            db, wanted, staged=src.candidates if src is not DIRECT_CODING else None
        )
        staged_kept, staged_scanned = dict(src.kept), dict(src.scanned)

    materials = sorted(groups.by_material)
    screened_materials = materials[:limit] if limit is not None else materials
    if limit is not None and len(materials) > len(screened_materials):
        logger.warning(
            "I08 coding candidates: screening %d of %d materials -- limit=%d. "
            "The remaining %d were NOT judged and are absent from the result.",
            len(screened_materials),
            len(materials),
            limit,
            len(materials) - len(screened_materials),
        )

    # One fingerprint per material, over the distinct text it is about to be
    # judged on. Built for the screened materials only -- there is no point
    # looking up a verdict for a material a limit has already excluded.
    fingerprints = {
        material: text_fingerprint(
            line.short_text for line in groups.by_material[material]
        )
        for material in screened_materials
    }
    stored = load_verdicts(db, fingerprints) if (use_model and reuse_stored) else {}

    candidates: list[CodingCandidate] = []
    reused = 0
    for material in screened_materials:
        material_lines = tuple(groups.by_material[material])
        in_universe = material in known_universe
        material_twins = twins.get(material, ())

        if not use_model:
            candidates.append(
                _unscreened(
                    material,
                    material_lines,
                    in_universe=in_universe,
                    twins=material_twins,
                    reason=(
                        "the model pass was not requested. The keyword screen, "
                        "the exclusions, the material split and the twin check "
                        "have all run -- only the language judgement is "
                        "outstanding."
                    ),
                )
            )
            continue

        already = stored.get(material)
        if already is not None:
            reused += 1
            candidates.append(
                _from_stored(
                    material,
                    material_lines,
                    already,
                    in_universe=in_universe,
                    twins=material_twins,
                )
            )
            continue

        candidate = screen_material(
            material,
            material_lines,
            in_universe=in_universe,
            twins=material_twins,
            cfg=cfg,
        )
        candidates.append(candidate)

        # UNSCREENED is never stored: a stub run, a missing credential and an
        # unreadable reply all land there, and caching any of them would turn
        # "nobody looked" into an answer nobody looks at again.
        if candidate.verdict != Verdict.UNSCREENED.value:
            save_verdict(
                db,
                material_id=material,
                fingerprint=fingerprints[material],
                verdict=candidate.verdict,
                confidence=candidate.confidence,
                reason=candidate.reason,
                model=candidate.model,
                provider=candidate.provider,
                prompt_version=candidate.prompt_version,
                screened_at=candidate.screened_at or datetime.now(timezone.utc),
            )

    candidates = rank_candidates(candidates)

    by_verdict: dict[str, int] = {}
    for candidate in candidates:
        by_verdict[candidate.verdict] = by_verdict.get(candidate.verdict, 0) + 1

    provider = next((c.provider for c in candidates if c.provider), "")
    model = next((c.model for c in candidates if c.model), "")

    stats = ScreenStats(
        sources=wanted,
        lines_with_text=sum(text_totals.values()),
        lines_with_text_by_source=text_totals,
        lines_with_repair_language=len(groups.all_lines),
        lines_by_source=lines_by_source,
        deleted_requisitions_dropped=deleted_dropped,
        converted_requisitions_dropped=converted_dropped,
        lines_already_eighty_series=len(groups.already_coded),
        lines_without_material=len(groups.without_material),
        lines_screened=sum(len(c.lines) for c in candidates),
        materials_screened=len(candidates),
        materials_found=len(materials),
        verdicts_reused=reused,
        corroborated=sum(1 for c in candidates if c.is_corroborated),
        by_verdict=by_verdict,
        keywords=cfg.repair_language_list,
        provider=provider,
        model=model,
        staged_rows_kept=staged_kept,
        staged_rows_scanned=staged_scanned,
    )

    logger.info(
        "I08 coding candidates (%s): %d lines with repair language of %d with "
        "text; %d already 80-series, %d with no material; %d PR lines dropped "
        "as deleted, %d as already converted; %d materials screened, %d "
        "verdicts reused %s",
        ",".join(wanted),
        stats.lines_with_repair_language,
        stats.lines_with_text,
        stats.lines_already_eighty_series,
        stats.lines_without_material,
        stats.deleted_requisitions_dropped,
        stats.converted_requisitions_dropped,
        stats.materials_screened,
        stats.verdicts_reused,
        by_verdict,
    )
    return candidates, stats
