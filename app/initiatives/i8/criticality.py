"""Criticality tier, behind one function.

W3.4 (Nishant's criticality module) is a dependency of the I08 universe read
model, and it was being pulled forward while W5.1 was being built. Waiting for
it would have blocked W5.1; importing it directly would have coupled this line
to someone else's delivery date. So it is read through one adapter instead.

When W3.4 lands, ``_load_from_w34`` gets a body and ``I8_CRITICALITY_SOURCE``
switches to ``w34``. Nothing above this module changes -- not the universe read
model, not the register, not the API. That is the entire point of the seam: it
turns another team's schedule into an implementation detail of one file.

The tiers are VZI's five-tier taxonomy, from the ZMM065 aging report:
NORMAL, OBSOLETE, CRITICAL, IMPACT, INSURANCE.

Never default an unknown tier. A material whose criticality we do not know must
come back as ``None`` and render as unknown. Showing it as NORMAL is a wrong
answer wearing a confident face, and the whole reason I08 exists is that people
act on what the screen says.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import normalise

logger = get_logger(__name__)

# The five tiers ZMM065 actually carries, measured across both plant reports.
# Anything outside this set is treated as unknown rather than passed through --
# a typo in a spreadsheet must not invent a sixth tier in the API.
KNOWN_TIERS: frozenset[str] = frozenset(
    {"NORMAL", "OBSOLETE", "CRITICAL", "IMPACT", "INSURANCE"}
)

# (material, plant) -> tier, with (material, None) as the any-plant fallback.
CriticalityMap = dict[tuple[str, str | None], str]


def _load_from_zmm065(db: Session) -> CriticalityMap:
    """Read the tiers from the ZMM065 aging report, both plants.

    The view unions Black Mountain and Gamsberg; reading only the Black
    Mountain sheet would give criticality for 87 of the 362 MARA 80-series
    materials and silently none at all for Gamsberg.
    """
    rows = db.execute(
        text(
            """
            select matnr, werks, criticality
            from v_zmm065
            where matnr is not null and criticality is not null
            """
        )
    ).all()

    mapping: CriticalityMap = {}
    for matnr, werks, criticality in rows:
        tier = criticality.strip().upper()
        if tier not in KNOWN_TIERS:
            continue
        mapping[(matnr, werks)] = tier
        # Any-plant fallback, for a material whose plant has no ZMM065 row.
        # First value wins; the per-plant entry above always takes precedence
        # when the exact plant is known, so this only ever fills a gap.
        mapping.setdefault((matnr, None), tier)
    return mapping


def _load_from_w34(db: Session) -> CriticalityMap:
    """Read the tiers from W3.4's criticality module.

    NOT IMPLEMENTED -- W3.4 had not landed when W5.1 closed. When it does, this
    is the only body that changes, and ``I8_CRITICALITY_SOURCE=w34`` switches to
    it. Until then, asking for it is a configuration error rather than a silent
    fall back to ZMM065: a deployment that thinks it is serving W3.4 tiers and
    is quietly serving last-July's spreadsheet is worse than one that refuses to
    start.
    """
    raise NotImplementedError(
        "I8_CRITICALITY_SOURCE=w34 is set, but W3.4's criticality module is not "
        "wired in yet. Point I8_CRITICALITY_SOURCE back at 'zmm065', or give "
        "_load_from_w34 a body -- see app/initiatives/i8/criticality.py."
    )


_LOADERS = {
    "zmm065": _load_from_zmm065,
    "w34": _load_from_w34,
}


def load_criticality(db: Session, cfg: I8Settings | None = None) -> CriticalityMap:
    """The whole criticality map, from whichever source is configured.

    Loaded in bulk rather than per material: the universe read model touches
    thousands of materials in one request, and a per-row lookup would be a
    query per row.
    """
    cfg = cfg or get_i8_settings()
    try:
        loader = _LOADERS[cfg.criticality_source]
    except KeyError:
        raise ValueError(
            f"Unknown I8_CRITICALITY_SOURCE={cfg.criticality_source!r}. "
            f"Known sources: {', '.join(sorted(_LOADERS))}."
        ) from None
    mapping = loader(db)
    logger.info(
        "I08 criticality: %d lookup entries (%d materials) from %s",
        len(mapping),
        sum(1 for _matnr, werks in mapping if werks is None),
        cfg.criticality_source,
    )
    return mapping


def criticality_for(
    mapping: CriticalityMap, matnr: str | None, werks: str | None = None
) -> str | None:
    """This material's tier at this plant, or None when unknown.

    Falls back from the exact plant to the any-plant entry, because ZMM065
    covers plant 1300 and 1500 while materials exist at 3000, 2000 and 1600 as
    well. Returns None rather than a default -- see the module docstring.
    """
    key = normalise(matnr)
    if key is None:
        return None
    if werks is not None and (key, werks) in mapping:
        return mapping[(key, werks)]
    return mapping.get((key, None))
