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
from enum import StrEnum
from typing import NamedTuple, Sequence

import numpy as np

from app.initiatives.i7.policy.thresholds import OarBlendPolicy

DAYS_PER_MONTH = 30.44

NOT_APPLIED = (
    "issue_size_floor: needs MSEG line items (not staged)",
    "project_or_order_exclusion: needs MSEG project / order fields (not staged)",
    "pack_rounding: no order-multiple source",
)


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


_LEAD_TIME_WORDS = {
    LeadTimeSource.MATERIAL_MASTER.value: "from the material master",
    LeadTimeSource.PLANT_DEFAULT.value: "assumed, plant default",
    LeadTimeSource.PEER_WEIGHTED_MEAN.value: "assumed, average of the similar materials",
}


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
        best_similarity=max((d.similarity for d in used), default=None),
        used_peer_count=len(used),
    )


# ---------------------------------------------------------------------------------------------------------
# words


def explain(result: BlendResult) -> str:
    """The split, in numbers, for the card -- in place of a vague "confidence may be thin"."""
    if not result.sized:
        return ""
    lead = (
        f"Lead time {result.lead_time_days:.0f} days ({_LEAD_TIME_WORDS.get(result.lead_time_source, 'source unknown')})."
        if result.lead_time_days is not None
        else ""
    )
    own_pct = round(100 * (result.w_own or 0.0))
    events = f"{result.n_events} consumption event{'s' if result.n_events != 1 else ''}"
    if result.status is BlendStatus.BLENDED:
        peer_pct = 100 - own_pct
        peers = f"{result.used_peer_count} similar material{'s' if result.used_peer_count != 1 else ''}"
        text = (
            f"Reorder point {result.rop} = {own_pct}% from this material's own {events} (estimate {math.ceil(result.rop_own - 1e-9)}) "
            f"+ {peer_pct}% from {peers} (estimate {math.ceil(result.rop_peer - 1e-9)}, best match {result.best_similarity:.2f})."
        )
    else:
        text = (
            f"Reorder point {result.rop} = 100% from this material's own {events} (estimate {math.ceil(result.rop_own - 1e-9)}); "
            "no similar material could be scaled to its demand."
        )
    tail = f" Safety stock {result.ss}, maximum stock {result.max_stock} (maximum = reorder point x {result.max_ratio:.2f})."
    flags = [g for g in result.guardrails_applied if g.startswith(("max_", "flag_"))]
    caution = f" Check: {'; '.join(flags)}." if flags else ""
    return f"{text} {lead}{tail}{caution}".replace("  ", " ").strip()
