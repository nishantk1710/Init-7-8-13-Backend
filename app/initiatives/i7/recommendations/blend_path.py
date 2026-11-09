"""The credibility-weighted blend, wired into recommendation generation.

For an OAR material-plant that qualifies for Min-Max (the unchanged conversion rule) AND whose similarity estimate
succeeded, this reads what :mod:`app.initiatives.i7.oar.blend` needs -- the material's own monthly consumption over
the last complete months, its similar materials with their SAP values and their own consumption, lead times -- in a
few grouped reads, and hands the builder one :class:`~app.initiatives.i7.oar.blend.BlendResult` per such material.
The estimator and the builder stay pure; nothing here changes eligibility or similarity.

A material that does not qualify for Min-Max keeps its similarity estimate: the blend scales the similar
materials' policies to the material's OWN demand, and a material with fewer than five issues has too little of it.

Entirely switched by ``I7_OAR_BLEND_ENABLED``. Callers pass nothing when it is off.
"""

from __future__ import annotations

import zlib
from datetime import date
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.initiatives.i7.features.consumption_window import WindowStatus, latest_run_window
from app.initiatives.i7.oar import blend as blend_module
from app.initiatives.i7.oar import exclusions as exclusions_module
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import builder, repository
from app.initiatives.i7.recommendations.types import ConversionEligibility

Key = tuple[str, str]


def last_complete_month(session: Session) -> date | None:
    """The latest month the newest feature run verified complete, or ``None``.

    ``None`` -- an unusable or unrecorded window -- is never read as "no consumption": the months cannot be
    vouched for, so no blend is made and the similarity estimate stands.
    """
    window = latest_run_window(session)
    if window is None or window.status != WindowStatus.USABLE.value or window.end is None:
        return None
    return date(window.end.year, window.end.month, 1)


def months_ending(last: date, count: int) -> list[date]:
    """The ``count`` first-of-month dates ending at ``last``, oldest first."""
    months: list[date] = []
    year, month = last.year, last.month
    for _ in range(count):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    months.reverse()
    return months


def seed_for(policy_seed: int, key: Key) -> int:
    """A per-material seed: reproducible, and different materials do not share one random stream."""
    return (policy_seed + zlib.crc32(f"{key[0]}/{key[1]}".encode())) % (2**31)


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def evaluate_rows(
    session: Session, rows: Iterable[Any], policy: PolicyDocument
) -> dict[Key, blend_module.BlendResult]:
    """One result per OAR material-plant among ``rows`` that qualifies for Min-Max with a successful similarity estimate.

    Anything else gets no entry. A material the blend cannot size (no consumption in the window, no lead time)
    gets an entry whose status says so; the builder then keeps the similarity numbers.
    """
    settings = policy.oar_blend
    wanted: list[Any] = []
    for row in rows:
        decision = builder.conversion_for(row, policy)
        if decision is None or decision.eligibility is not ConversionEligibility.ELIGIBLE:
            continue
        if row.estimate_status != "SUCCESS":
            continue
        wanted.append(row)
    if not wanted:
        return {}

    last_month = last_complete_month(session)
    months = months_ending(last_month, settings.window_months) if last_month is not None else []
    monthly_by_key = (
        repository.load_monthly_consumption(session, months[0], months[-1]) if months else {}
    )

    similarity = policy.similarity
    neighbours_by_run: dict[int, dict] = {}
    lead_times_by_run: dict[int, dict] = {}
    exclusions = exclusions_module.load_active(session)

    results: dict[Key, blend_module.BlendResult] = {}
    for row in wanted:
        key = (row.sap_material_number, row.sap_plant_code)
        if row.oar_run_id not in neighbours_by_run:
            neighbours_by_run[row.oar_run_id] = repository.load_oar_neighbours(session, row.oar_run_id)
        if row.feature_run_id not in lead_times_by_run:
            lead_times_by_run[row.feature_run_id] = repository.load_feature_lead_times(session, row.feature_run_id)
        lead_times = lead_times_by_run[row.feature_run_id]

        removed = exclusions_module.excluded_for(exclusions, *key)
        peers: list[blend_module.PeerInput] = []
        for n in neighbours_by_run[row.oar_run_id].get(key, []):
            donor = (n.neighbour_material, n.neighbour_plant)
            if donor in removed or not n.inventory_eligible or n.combined_similarity is None:
                continue
            if float(n.combined_similarity) < similarity.minimum_similarity:
                continue
            donor_monthly = monthly_by_key.get(donor)
            annual = float(sum(donor_monthly.values())) if donor_monthly else 0.0
            peers.append(
                blend_module.PeerInput(
                    material=n.neighbour_material,
                    plant=n.neighbour_plant,
                    similarity=float(n.combined_similarity),
                    rop=_float(n.rop),
                    safety_stock=_float(n.safety_stock),
                    max_stock=_float(n.max_stock),
                    annual_consumption=annual,
                    lead_time_days=_float(lead_times.get(donor)),
                )
            )
        peers = peers[: similarity.maximum_neighbours]

        own = monthly_by_key.get(key, {})
        monthly = [float(own.get(month, 0) or 0) for month in months] if months else None
        master_days = getattr(row, "lead_time_days", None)
        if master_days is None:
            master_days = getattr(row, "planned_delivery_time_days", None)

        results[key] = blend_module.estimate(
            blend_module.BlendInput(
                monthly=monthly,
                n_events=int(row.consumption_count_12m or 0),
                lead_time_days=_float(master_days),
                peers=peers,
                unit_price=_float(getattr(row, "unit_price", None)),
            ),
            settings,
            seed_for(settings.seed, key),
        )
    return results
