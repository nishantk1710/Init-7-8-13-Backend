"""Credibility-weighted ROP / SS / Max for an OAR material that qualifies for Min-Max.

PURE: no database, no SAP, no clock. Everything comes in through :class:`BlendInput` and the policy; the same input
and seed always give the same result. :mod:`app.initiatives.i7.recommendations.blend_path` does the reading.

THE PROBLEM THIS ANSWERS

The similarity estimate copies the similar materials' own SAP settings. Those reflect THEIR demand and planning
habits, so a busier peer's scale leaks into the answer (WEDGE BELTS 5000034258 uses 30 units a year and got a ROP
of 36), and the material's own 12-month history -- which made it eligible -- is never used to size it.

THE METHOD

1. **Own estimate.** The material's monthly buckets, zeros included. Lead-time demand is bootstrapped (buckets
   sampled with replacement, summed); ``ROP_own`` is its empirical percentile (85 -> the 85% service level, z =
   1.04, on the cards) and ``SS_own = max(0, ROP_own - mean lead-time demand)``. The empirical percentile, not
   mean + z * sigma: these series are intermittent and the normal approximation understates the tail.
2. **Peer estimate, scale-free.** Each similar material's policy becomes ratios -- months of cover
   (``ROP / monthly use``), SS in months, and Max / ROP -- averaged with a steep similarity weight
   ``max(0, sim - floor) ** exponent``, then projected onto THIS material's monthly use. Peers supply the shape
   of the policy; the material supplies the scale.
3. **Credibility.** ``W_own = n / (n + k * q)``: ``n`` the issue transactions in the window, ``q`` the
   similarity-weighted mean match squared. A stronger own history, or weaker peers, moves weight to the material's
   own estimate (Buhlmann-style credibility / shrinkage).
4. **Blend.** ROP and SS are blended; Max is DERIVED (``ROP x max(peer Max/ROP, floor)``) so the policy stays
   coherent rather than three numbers that were each blended on their own.
5. **Guardrails** after blending: Max never above the year's use, a flag above ``max_cover_flag_months`` of cover,
   optional tighter price-band caps, whole units.

NOT APPLIED -- and stated in every result: the issue-size floor and the project / work-order exclusion (they need
MSEG line items, which are not staged) and rounding to a pack size (no source for it). Nothing here pretends to
have applied them.

A material this cannot size (no consumption in the window, no lead time) gets a result whose status says so; the
caller then keeps the similarity estimate it already had.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import NamedTuple, Sequence

import numpy as np

from app.initiatives.i7.policy.thresholds import OarBlendPolicy

DAYS_PER_MONTH = 30.44

_PROJECT_EXCLUSION_NOT_APPLIED = (
    "project_or_order_exclusion: the movement data has no project / WBS field and the order number carries no "
    "order type, so a campaign draw cannot be told from routine maintenance"
)
_PACK_ROUNDING_NOT_APPLIED = "pack_rounding: no order-multiple source"

NOT_APPLIED = (
    "issue_size_floor: no single-issue lines were available for this material",
    _PROJECT_EXCLUSION_NOT_APPLIED,
    _PACK_ROUNDING_NOT_APPLIED,
)
"""Stated in every result. The issue-size floor leaves this list when it was worked out (see ``NOT_APPLIED_WITH_FLOOR``)."""

NOT_APPLIED_WITH_FLOOR = (_PROJECT_EXCLUSION_NOT_APPLIED, _PACK_ROUNDING_NOT_APPLIED)


class BlendStatus(StrEnum):
    BLENDED = "BLENDED"
    """Own and similar-material estimates both contributed."""
    OWN_ONLY = "OWN_ONLY"
    """No similar material was usable (no use, or no ROP), so the own estimate stands alone."""
    NO_CONSUMPTION = "NO_CONSUMPTION"
    """Nothing consumed in the window: nothing to scale a recommendation to. Not read as zero need."""
    NO_LEAD_TIME = "NO_LEAD_TIME"
    """No lead time from any step of the fallback chain."""
    NO_BUCKETS = "NO_BUCKETS"
    """The window's months are not known (unusable consumption window)."""


class LeadTimeSource(StrEnum):
    MATERIAL_MASTER = "MATERIAL_MASTER"
    PLANT_DEFAULT = "PLANT_DEFAULT"
    PEER_WEIGHTED_MEAN = "PEER_WEIGHTED_MEAN"


_LEAD_TIME_ASSUMED_WORDS = {
    LeadTimeSource.PLANT_DEFAULT.value: "assumed: plant default",
    LeadTimeSource.PEER_WEIGHTED_MEAN.value: "assumed: average of its similar materials",
}
"""A lead time kept for the material itself needs no note. One that was assumed is said to be."""


class PeerInput(NamedTuple):
    """One similar material, as the estimator needs it."""

    material: str
    plant: str
    similarity: float
    rop: float | None
    safety_stock: float | None
    max_stock: float | None
    annual_consumption: float | None
    lead_time_days: float | None = None


class IssueLine(NamedTuple):
    """One movement line for a material-plant: an issue (201 / 261) or its reversal (202 / 262)."""

    movement_type: str
    quantity: float
    reservation: str | None
    posted: date


def net_issue_sizes(
    lines: Sequence[IssueLine], issue_types: frozenset[str], reversal_types: frozenset[str]
) -> list[float]:
    """The size of each SINGLE issue, with cancelled ones removed.

    A reversal cancels the earliest not-yet-cancelled issue of the same quantity on the same reservation (a blank
    reservation matches a blank one). That matching rule is an assumption -- the line data carries no direct link
    between an issue and its reversal. A reversal with nothing to cancel (its issue predates the window) is ignored.
    """
    issues = sorted((ln for ln in lines if ln.movement_type in issue_types), key=lambda ln: ln.posted)
    cancelled = [False] * len(issues)
    for rev in sorted((ln for ln in lines if ln.movement_type in reversal_types), key=lambda ln: ln.posted):
        for i, issue in enumerate(issues):
            if not cancelled[i] and issue.quantity == rev.quantity and (issue.reservation or "") == (rev.reservation or ""):
                cancelled[i] = True
                break
    return [issue.quantity for i, issue in enumerate(issues) if not cancelled[i] and issue.quantity > 0]


@dataclass(frozen=True)
class PeerDetail:
    material: str
    plant: str
    similarity: float
    weight: float
    r_rop: float | None
    r_ss: float | None
    r_max: float | None
    used: bool


@dataclass(frozen=True)
class BlendInput:
    monthly: Sequence[float] | None
    """The window's monthly buckets in order, zeros included; ``None`` when the window cannot be vouched for."""
    n_events: int
    """Net issue transactions in the window (the Min-Max usage count)."""
    lead_time_days: float | None
    peers: Sequence[PeerInput]
    unit_price: float | None = None
    issue_sizes: Sequence[float] | None = None
    """The sizes of the material's single issues in the window (:func:`net_issue_sizes`); ``None`` when no lines
    were read. Drives the issue-size floor."""
    eoq: float | None = None
    """No EOQ source exists yet; when one does, Max uses ``max(EOQ, ratio step)``."""


@dataclass(frozen=True)
class BlendResult:
    status: BlendStatus
    rop: int | None = None
    ss: int | None = None
    max_stock: int | None = None
    rop_own: float | None = None
    ss_own: float | None = None
    rop_peer: float | None = None
    ss_peer: float | None = None
    max_ratio: float | None = None
    w_own: float | None = None
    w_peer: float | None = None
    peer_quality_q: float | None = None
    n_events: int = 0
    annual_consumption: float | None = None
    lead_time_days: float | None = None
    lead_time_source: str | None = None
    estimator: str = "bootstrap"
    lead_time_bucket_mode: str = "whole_month"
    guardrails_applied: tuple[str, ...] = ()
    guardrails_not_applied: tuple[str, ...] = NOT_APPLIED
    peers: tuple[PeerDetail, ...] = ()
    best_similarity: float | None = None
    used_peer_count: int = 0
    issue_size_floor: float | None = None
    """The percentile of single-issue sizes the reorder point may not fall below, when it was worked out."""
    issue_count: int = 0

    @property
    def sized(self) -> bool:
        return self.status in (BlendStatus.BLENDED, BlendStatus.OWN_ONLY)


# ---------------------------------------------------------------------------------------------------------
# lead time


def resolve_lead_time(
    master_days: float | None,
    plant_default_days: float | None,
    peers: Sequence[PeerInput],
    *,
    floor: float,
    exponent: float,
) -> tuple[float | None, str | None]:
    """The fallback chain, and which step answered: material master -> plant default -> similar materials' mean.

    A category median is a step in the brief, but no category lead-time data exists here, so it is not
    invented: the chain goes straight on to the similar materials' similarity-weighted mean. ``(None, None)`` when
    nothing answers.
    """
    if master_days is not None and master_days > 0:
        return float(master_days), LeadTimeSource.MATERIAL_MASTER.value
    if plant_default_days is not None and plant_default_days > 0:
        return float(plant_default_days), LeadTimeSource.PLANT_DEFAULT.value
    weighted = [
        (max(0.0, p.similarity - floor) ** exponent, p.lead_time_days)
        for p in peers
        if p.lead_time_days is not None and p.lead_time_days > 0
    ]
    total = sum(w for w, _ in weighted)
    if total > 0:
        return sum(w * d for w, d in weighted) / total, LeadTimeSource.PEER_WEIGHTED_MEAN.value
    plain = [d for _, d in weighted]
    if plain:
        return sum(plain) / len(plain), LeadTimeSource.PEER_WEIGHTED_MEAN.value
    return None, None


# ---------------------------------------------------------------------------------------------------------
# own estimate


def _buckets_for_lead_time(lead_time_days: float, mode: str) -> tuple[int, float]:
    """(how many monthly buckets are summed, the scale applied to that sum)."""
    months = lead_time_days / DAYS_PER_MONTH
    count = max(1, math.ceil(months))
    scale = months / count if mode == "prorated" else 1.0
    return count, scale


def own_estimate(
    monthly: Sequence[float], lead_time_days: float, config: OarBlendPolicy, seed: int
) -> tuple[float, float, float]:
    """``(ROP_own, SS_own, mean lead-time demand)`` from the material's own buckets."""
    values = np.asarray([max(float(v), 0.0) for v in monthly], dtype=float)
    count, scale = _buckets_for_lead_time(lead_time_days, config.lead_time_bucket_mode)
    rng = np.random.default_rng(seed)
    draws = rng.choice(values, size=(config.bootstrap_draws, count), replace=True).sum(axis=1) * scale
    mean_demand = float(draws.mean())
    rop = float(np.percentile(draws, config.service_percentile))
    if config.own_estimator == "croston_sba":
        # Mean demand rate by Croston / SBA: mean issue size z over mean inter-arrival time p (months), bias-corrected.
        # The spread above the mean stays the bootstrap's, so only the centre moves.
        nonzero = values[values > 0]
        if nonzero.size == 0:
            rate = 0.0
        else:
            z = float(nonzero.mean())
            p = len(values) / nonzero.size
            rate = (z / p) * (1.0 - config.croston_alpha / 2.0)
        months = lead_time_days / DAYS_PER_MONTH
        croston_mean = rate * months
        rop = croston_mean + (rop - mean_demand)
        mean_demand = croston_mean
    return rop, max(0.0, rop - mean_demand), mean_demand


# ---------------------------------------------------------------------------------------------------------
# peer estimate


def _peer_details(peers: Sequence[PeerInput], config: OarBlendPolicy) -> list[PeerDetail]:
    details = []
    for peer in peers:
        weight = max(0.0, peer.similarity - config.similarity_floor) ** config.similarity_exponent
        usable = (
            peer.annual_consumption is not None
            and peer.annual_consumption > 0
            and peer.rop is not None
            and peer.rop > 0
            and peer.safety_stock is not None
            and peer.max_stock is not None
        )
        if usable:
            monthly_use = peer.annual_consumption / 12.0
            r_rop, r_ss, r_max = peer.rop / monthly_use, peer.safety_stock / monthly_use, peer.max_stock / peer.rop
        else:
            r_rop = r_ss = r_max = None
        details.append(
            PeerDetail(peer.material, peer.plant, peer.similarity, weight, r_rop, r_ss, r_max, bool(usable and weight > 0))
        )
    return details


def _without_outliers(sizes: list[float], multiple: float | None, guardrails: list[str]) -> list[float]:
    """Optionally set aside a single issue far larger than the typical one (OFF unless configured)."""
    if multiple is None or len(sizes) < 3:
        return sizes
    median = float(np.median(sizes))
    kept = [v for v in sizes if v <= multiple * median]
    if kept and len(kept) < len(sizes):
        guardrails.append(f"issue_outliers_ignored: {len(sizes) - len(kept)}")
        return kept
    return sizes


# ---------------------------------------------------------------------------------------------------------
# the blend


def estimate(inputs: BlendInput, config: OarBlendPolicy, seed: int) -> BlendResult:
    base = dict(
        n_events=inputs.n_events,
        estimator=config.own_estimator,
        lead_time_bucket_mode=config.lead_time_bucket_mode,
    )
    if inputs.monthly is None or len(inputs.monthly) == 0:
        return BlendResult(BlendStatus.NO_BUCKETS, **base)
    monthly = [max(float(v), 0.0) for v in inputs.monthly]
    annual = sum(monthly)
    if annual <= 0:
        return BlendResult(BlendStatus.NO_CONSUMPTION, annual_consumption=0.0, **base)

    lead_days, lead_source = resolve_lead_time(
        inputs.lead_time_days,
        config.plant_default_lead_time_days,
        inputs.peers,
        floor=config.similarity_floor,
        exponent=config.similarity_exponent,
    )
    if lead_days is None:
        return BlendResult(BlendStatus.NO_LEAD_TIME, annual_consumption=annual, **base)

    monthly_use = annual / len(monthly)
    rop_own, ss_own, _ = own_estimate(monthly, lead_days, config, seed)

    details = _peer_details(inputs.peers, config)
    used = [d for d in details if d.used]
    guardrails: list[str] = []

    if used:
        weight_sum = sum(d.weight for d in used)
        r_rop = sum(d.weight * d.r_rop for d in used) / weight_sum
        r_ss = sum(d.weight * d.r_ss for d in used) / weight_sum
        r_max = sum(d.weight * d.r_max for d in used) / weight_sum
        quality = (sum(d.weight * d.similarity for d in used) / weight_sum) ** 2
        rop_peer, ss_peer = r_rop * monthly_use, r_ss * monthly_use
        w_own = inputs.n_events / (inputs.n_events + config.credibility_k * quality) if inputs.n_events > 0 else 0.0
        rop = w_own * rop_own + (1.0 - w_own) * rop_peer
        ss = w_own * ss_own + (1.0 - w_own) * ss_peer
        ratio = max(r_max, config.max_ratio_floor)
        status = BlendStatus.BLENDED
    else:
        # No usable similar material: the own estimate stands alone, and the floor ratio sizes the max.
        rop_peer = ss_peer = quality = r_max = None
        w_own = 1.0
        rop, ss = rop_own, ss_own
        ratio = config.max_ratio_floor
        status = BlendStatus.OWN_ONLY
        guardrails.append("no_usable_similar_material: own estimate only")

    # The reorder point may not be smaller than a normal single issue. Applied before Max is derived, so the
    # policy stays coherent (Max follows the raised ROP). SS is left as it was: it stays below the ROP.
    sizes = [float(v) for v in (inputs.issue_sizes or ()) if v and float(v) > 0]
    floor_value: float | None = None
    if sizes and config.issue_size_floor_percentile is not None:
        counted = _without_outliers(sizes, config.issue_outlier_multiple, guardrails)
        floor_value = float(np.percentile(counted, config.issue_size_floor_percentile))
        if rop < floor_value:
            guardrails.append(f"rop_raised_to_issue_size_p{config.issue_size_floor_percentile:g}")
            rop = floor_value

    step = max(inputs.eoq or 0.0, rop * (ratio - 1.0))
    max_stock = rop + step

    # --- guardrails, after blending ---------------------------------------------------------------------
    if max_stock > annual:
        max_stock = annual
        guardrails.append("max_capped_at_annual_consumption")
    if inputs.unit_price is not None:
        for band in sorted(config.max_cap_by_price, key=lambda b: b.min_unit_price, reverse=True):
            if inputs.unit_price >= band.min_unit_price:
                cap = band.max_months_cover * monthly_use
                if max_stock > cap:
                    max_stock = cap
                    guardrails.append(f"max_capped_by_price_band_{band.min_unit_price:g}")
                break
    if max_stock < rop:
        max_stock = rop
        guardrails.append("max_raised_to_rop")
    if max_stock > config.max_cover_flag_months * monthly_use:
        guardrails.append(f"flag_max_above_{config.max_cover_flag_months:g}_months_of_cover")

    rop_i, ss_i, max_i = math.ceil(rop - 1e-9), math.ceil(ss - 1e-9), math.ceil(max_stock - 1e-9)
    max_i = max(max_i, rop_i)
    return BlendResult(
        status=status,
        rop=rop_i,
        ss=ss_i,
        max_stock=max_i,
        rop_own=rop_own,
        ss_own=ss_own,
        rop_peer=rop_peer,
        ss_peer=ss_peer,
        max_ratio=ratio,
        w_own=w_own,
        w_peer=1.0 - w_own,
        peer_quality_q=quality,
        n_events=inputs.n_events,
        annual_consumption=annual,
        lead_time_days=lead_days,
        lead_time_source=lead_source,
        estimator=config.own_estimator,
        lead_time_bucket_mode=config.lead_time_bucket_mode,
        guardrails_applied=tuple(guardrails),
        peers=tuple(details),
        guardrails_not_applied=NOT_APPLIED_WITH_FLOOR if floor_value is not None else NOT_APPLIED,
        best_similarity=max((d.similarity for d in used), default=None),
        used_peer_count=len(used),
        issue_size_floor=floor_value,
        issue_count=len(sizes),
    )


# ---------------------------------------------------------------------------------------------------------
# words


def _units(value: float | int) -> str:
    whole = int(value)
    return f"{whole} unit" if whole == 1 else f"{whole} units"


def _applied(result: BlendResult, prefix: str) -> str | None:
    return next((g for g in result.guardrails_applied if g.startswith(prefix)), None)


def explain_split(result: BlendResult) -> str:
    """One line: where the reorder level comes from, in percentages. For the list of drivers."""
    if not result.sized:
        return ""
    own_pct = round(100 * (result.w_own or 0.0))
    if result.status is BlendStatus.BLENDED:
        peers = f"{result.used_peer_count} similar material{'s' if result.used_peer_count != 1 else ''}"
        return f"Reorder level {result.rop}: {own_pct}% from this material's own usage, {100 - own_pct}% from {peers}."
    return f"Reorder level {result.rop}: from this material's own usage; no similar material could be scaled to its demand."


def explain(result: BlendResult) -> str:
    """The recommendation in plain words, one short paragraph per line. Built from the figures, not written by a model.

    Replaces the vague "confidence may be thin": it says how much came from the material's own usage and how much
    from similar materials, what was done to the numbers afterwards, and the lead time used. A guardrail is said
    in a sentence, never as its code.
    """
    if not result.sized:
        return ""
    own_pct = round(100 * (result.w_own or 0.0))
    lines: list[str] = []

    if result.status is BlendStatus.BLENDED:
        peer_pct = 100 - own_pct
        peers = f"{result.used_peer_count} similar material{'s' if result.used_peer_count != 1 else ''}"
        if own_pct >= peer_pct:
            basis = f"It is mainly based on this material's own usage ({own_pct}%), with some reference from {peers} ({peer_pct}%)."
        else:
            basis = f"It is mainly based on {peers} ({peer_pct}%), with some reference from this material's own usage ({own_pct}%)."
    else:
        basis = "It is based on this material's own usage; no similar material could be scaled to its demand."
    lines.append(f"The recommended reorder level is {_units(result.rop)}. {basis}")

    if _applied(result, "rop_raised_to_issue_size") and result.issue_size_floor is not None:
        lines.append(
            f"The reorder level was raised to {result.rop} so that it covers one normal single issue of this material "
            f"(about {_units(round(result.issue_size_floor))})."
        )
    ignored = _applied(result, "issue_outliers_ignored")
    if ignored:
        count = int(ignored.split(":")[1])
        lines.append(
            f"{count} unusually large single issue{'s were' if count != 1 else ' was'} left out when working out what a normal issue is."
        )

    stock = (
        "No extra safety stock is kept."
        if result.ss == 0
        else f"A safety stock of {_units(result.ss)} is kept to protect against unexpected demand or delays in supply."
    )
    maximum = f"The maximum stock is {_units(result.max_stock)}, so there is enough stock without overstocking."
    notes = []
    if _applied(result, "max_capped_at_annual_consumption"):
        notes.append("It is limited to one year of this material's usage.")
    if _applied(result, "max_capped_by_price_band"):
        notes.append("It is limited because of this material's high unit price.")
    if _applied(result, "max_raised_to_rop"):
        notes.append("It was raised to match the reorder level.")
    cover = _applied(result, "flag_max_above_")
    if cover:
        months = cover.removeprefix("flag_max_above_").split("_months")[0]
        notes.append(f"That is more than {months} months of this material's usage, so it is worth a check.")
    lines.append(" ".join([stock, maximum, *notes]))

    if result.lead_time_days is not None:
        assumed = _LEAD_TIME_ASSUMED_WORDS.get(result.lead_time_source)
        lines.append(
            f"The calculation also uses a lead time of {result.lead_time_days:.0f} days" + (f" ({assumed})." if assumed else ".")
        )
    return "\n".join(lines)
