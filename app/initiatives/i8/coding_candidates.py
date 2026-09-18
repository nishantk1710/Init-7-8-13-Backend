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
Measured against the July extract on 15-Sep. The task plan's three headline
numbers are exactly right::

    82,718   PO lines carrying free text (every line in EKPO has some)
       305   non-80-series lines with repair language
         6   80-series lines with repair language

But **305 is not the screening population**, and this is the finding that
reshaped the module:

    183 of the 305 (60%) have NO MATERIAL NUMBER AT ALL.

A coding candidate is a material that *should* have been coded 80-series and was
not. A PO line with no material cannot be mis-coded -- there is nothing to
re-code. Those 183 are free-text service purchases, and they are a real and
separate finding (repair spend happening entirely outside the material master),
so they are counted and reported rather than quietly dropped. They are never
candidates.

That leaves 122 lines carrying a material, and those collapse to **41 distinct
materials**, because the same part is bought repeatedly::

    5000075445  18 lines      5000075446  18 lines      5000024401  16 lines

So the screen judges **materials, not lines**. That is both cheaper -- 41 model
calls rather than 305 -- and more correct: the coding is a property of the
material, so judging the same part four times could produce four verdicts and no
way to choose between them. Every line's text is passed in together, which is
also strictly more evidence per call.

A caution the plan did not have: roughly 57 of the 305 are repair *products* --
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

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ai import AIError, Completion
from app.core.logging import get_logger
from app.core.prompts import PromptError, complete_with_prompt
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series

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
    the keyword and is correctly not 80-series. Roughly 57 of the 305 screened
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


@dataclass(frozen=True)
class CandidateLine:
    """One PO line whose free text mentions repair."""

    purchasing_document: str
    item: str
    material_id: str | None
    """None on 183 of the 305 matching lines -- a free-text service purchase."""

    plant: str | None
    short_text: str
    matched: tuple[str, ...]
    """Which configured keywords this text hit. Carried so a reader can see why
    the line was picked up without re-running the regex in their head."""

    raised_at: date | None
    item_category: str | None


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

    lines_with_text: int
    lines_with_repair_language: int
    lines_already_eighty_series: int
    """Repair language on a material that IS coded repairable. Not a candidate
    -- the convention worked. Measured at 6."""

    lines_without_material: int
    """Free-text service purchases. Cannot be mis-coded because there is nothing
    to re-code. Measured at 183, which is 60% of the matches."""

    lines_screened: int
    materials_screened: int
    materials_found: int = 0
    """How many materials the keyword pass produced. Differs from
    ``materials_screened`` only when a limit truncated the run, and the two are
    reported separately so a truncated screen cannot read as a complete one."""

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
# The keyword regex is a BIND PARAMETER built from configuration, never
# interpolated -- so there is still no repair vocabulary written into a query.
#
# Note what is NOT here: any exclusion of 80-series materials. The prefilter
# contract (material_number.series_like_patterns) says the database may only
# ever be LOOSER than the Python predicate, and `NOT LIKE '80%'` would be
# stricter -- it would drop a material like '80' that the real predicate
# rejects anyway, but also anything the predicate would have kept. The set is
# ~311 rows, so it is filtered in Python where the one true test lives.
_CANDIDATE_SQL = """
select
    p.ebeln, p.ebelp, p.matnr, p.werks, p.txz01, p.erdat, p.pstyp
from v_ekpo p
where coalesce(p.txz01, '') <> ''
  and p.txz01 ~* :pattern
order by p.ebeln, p.ebelp
"""

_TEXT_TOTAL_SQL = "select count(*) from v_ekpo where coalesce(txz01, '') <> ''"


def matched_keywords(short_text: str, cfg: I8Settings) -> tuple[str, ...]:
    """Which configured keywords this text hits. Case-insensitive substrings."""
    lowered = (short_text or "").lower()
    return tuple(word for word in cfg.repair_language_list if word in lowered)


def fetch_repair_language_lines(
    db: Session, cfg: I8Settings | None = None
) -> list[CandidateLine]:
    """Every PO line whose free text mentions repair, coded or not.

    The keyword pass, and nothing more. It does not decide anything: the
    80-series test and the model both run over what this returns.
    """
    cfg = cfg or get_i8_settings()
    rows = db.execute(text(_CANDIDATE_SQL), {"pattern": cfg.repair_language_pattern}).mappings().all()

    return [
        CandidateLine(
            purchasing_document=row["ebeln"],
            item=row["ebelp"],
            material_id=row["matnr"],
            plant=row["werks"],
            short_text=row["txz01"],
            matched=matched_keywords(row["txz01"], cfg),
            raised_at=row["erdat"],
            item_category=row["pstyp"],
        )
        for row in rows
    ]


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

    This is where the population shrinks from 305 to 41, and the reason is
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
# Raw string: the regex needs a literal backslash-s to reach Postgres.
_TWIN_SQL = r"""
with texts as (
    select
        matnr,
        upper(regexp_replace(btrim(txz01), '\s+', ' ', 'g')) as normalised,
        min(txz01) as sample
    from v_ekpo
    where matnr is not null and coalesce(txz01, '') <> ''
    group by 1, 2
)
select distinct coded.matnr as coded_material, coded.sample as shared_text,
       suspect.matnr as suspect_material
from texts coded
join texts suspect on suspect.normalised = coded.normalised
where coded.matnr = any(:coded)
  and suspect.matnr = any(:suspects)
"""


def find_twins(
    db: Session, materials, cfg: I8Settings | None = None
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Suspect material -> the 80-series materials sharing its exact text.

    The corroboration described in the module docstring, and the only part of
    this screen that produces evidence rather than an opinion. One query for
    every candidate at once.

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
                text("select distinct matnr from v_ekpo where matnr is not null")
            ).scalars()
            if is_eighty_series(row, cfg)
        }
    )
    if not coded:
        return {}

    rows = (
        db.execute(text(_TWIN_SQL), {"coded": coded, "suspects": suspects})
        .mappings()
        .all()
    )

    found: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        found.setdefault(row["suspect_material"], []).append(
            (row["coded_material"], row["shared_text"])
        )

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


def screen(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    limit: int | None = None,
    universe_materials: set[str] | None = None,
    use_model: bool = True,
) -> tuple[list[CodingCandidate], ScreenStats]:
    """The whole screen: keyword pass, split, then one model call per material.

    ``use_model=False`` runs everything EXCEPT the model: the keyword pass, the
    split, and the twin corroboration -- all of which are deterministic, need no
    credentials and take under a second. Every material comes back UNSCREENED,
    which is the honest description of what happened.

    That split exists because the model pass is slow enough to matter. Measured
    on 15-Sep against live gpt-4o: **41 materials, 246 seconds**, roughly six
    seconds each and strictly sequential. That is fine for a batch and far too
    slow for a page load, so the API defaults to the fast half and asks for the
    model explicitly.

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

    lines = fetch_repair_language_lines(db, cfg)
    groups = partition(lines, cfg)
    known_universe = universe_materials or set()
    # Computed BEFORE the model runs, for all candidates at once. It is
    # deterministic, so it is worth having even when no model answers: a
    # stub-provider run still reports which suspects SAP itself contradicts.
    twins = find_twins(db, groups.by_material, cfg)

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

    candidates = [
        screen_material(
            material,
            groups.by_material[material],
            in_universe=material in known_universe,
            twins=twins.get(material, ()),
            cfg=cfg,
        )
        if use_model
        else _unscreened(
            material,
            groups.by_material[material],
            in_universe=material in known_universe,
            twins=twins.get(material, ()),
            reason=(
                "the model pass was not requested. The keyword screen, the "
                "material split and the twin check have all run -- only the "
                "language judgement is outstanding."
            ),
        )
        for material in screened_materials
    ]

    by_verdict: dict[str, int] = {}
    for candidate in candidates:
        by_verdict[candidate.verdict] = by_verdict.get(candidate.verdict, 0) + 1

    lines_with_text = db.execute(text(_TEXT_TOTAL_SQL)).scalar() or 0
    provider = next((c.provider for c in candidates if c.provider), "")
    model = next((c.model for c in candidates if c.model), "")

    stats = ScreenStats(
        lines_with_text=int(lines_with_text),
        lines_with_repair_language=len(groups.all_lines),
        lines_already_eighty_series=len(groups.already_coded),
        lines_without_material=len(groups.without_material),
        lines_screened=sum(len(c.lines) for c in candidates),
        materials_screened=len(candidates),
        materials_found=len(materials),
        corroborated=sum(1 for c in candidates if c.is_corroborated),
        by_verdict=by_verdict,
        keywords=cfg.repair_language_list,
        provider=provider,
        model=model,
    )

    logger.info(
        "I08 coding candidates: %d lines with repair language of %d with text; "
        "%d already 80-series, %d with no material; %d materials screened %s",
        stats.lines_with_repair_language,
        stats.lines_with_text,
        stats.lines_already_eighty_series,
        stats.lines_without_material,
        stats.materials_screened,
        by_verdict,
    )
    return candidates, stats
