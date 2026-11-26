"""The conversion cooldown: leave a material alone for a while after it changes between Min-Max and OAR.

THE RULE. After a material-plant changes planning group -- OAR (MRP type ND or PD) to Min-Max (VM or VB), or the
other way -- it is not recommended for conversion back for ``cooldown_months`` (12 to begin with, set by
``Settings.i7_conversion_cooldown_months``, 0 = off). This stops a material bouncing between the two as its usage
drifts across a threshold.

HOW A CHANGE IS SEEN. SAP keeps no history of a material's MRP type, so the pipeline's ``reverse_conversion`` stage
keeps its own: ``i7_planning_group_observation`` holds the group each material-plant was last seen in. A run compares
the group it sees now; when it differs, the row is stamped ``changed_on`` = the day of that run. A change is therefore
dated from the run that noticed it, which may be a little after SAP changed. A material seen for the first time has no
``changed_on``: we cannot know when it last changed, so it has no cooldown until a change is actually seen.

This module decides nothing about eligibility beyond "is it cooling down"; the rest is ``evaluate``. The pure
functions need no database; the three at the bottom read and write the observation table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy
from app.initiatives.i7.reverse_conversion.metrics import add_months, first_of_month
from app.models.i7_features import MaterialFeature
from app.models.i7_reverse import PlanningGroupObservation

MIN_MAX = "MIN_MAX"
OAR = "OAR"

Key = tuple[str, str]


def planning_group(mrp_type: str | None, min_max_types: frozenset[str], oar_types: frozenset[str]) -> str | None:
    """``MIN_MAX`` or ``OAR`` for an MRP type, or None for any other (such a material is in neither group)."""
    code = (mrp_type or "").strip().upper()
    if code in min_max_types:
        return MIN_MAX
    if code in oar_types:
        return OAR
    return None


@dataclass(frozen=True)
class Observation:
    group: str
    first_seen_on: date
    changed_on: date | None


@dataclass(frozen=True)
class ObservationChanges:
    """What a run has to write: rows to add, and rows whose group changed (with the day it was seen)."""

    new: dict[Key, Observation]
    changed: dict[Key, Observation]


def observe(previous: dict[Key, Observation], current: dict[Key, str], today: date) -> ObservationChanges:
    """Compare today's groups with the stored ones.

    * not stored before -> a new row, first seen today, ``changed_on`` unknown (None);
    * stored, same group -> nothing to write;
    * stored, different group -> the group is updated and ``changed_on`` is today.

    A material-plant that is no longer in ``current`` (left the scope, or its MRP type is another one) keeps its
    stored row untouched: it is not evidence of a change."""
    new: dict[Key, Observation] = {}
    changed: dict[Key, Observation] = {}
    for key, group in current.items():
        before = previous.get(key)
        if before is None:
            new[key] = Observation(group=group, first_seen_on=today, changed_on=None)
        elif before.group != group:
            changed[key] = Observation(group=group, first_seen_on=before.first_seen_on, changed_on=today)
    return ObservationChanges(new=new, changed=changed)


def cooldown_ends(changed_on: date | None, cooldown_months: int) -> date | None:
    """The first day the material may be recommended again, or None when there is no cooldown to wait out."""
    if changed_on is None or cooldown_months <= 0:
        return None
    return add_months(first_of_month(changed_on), cooldown_months)


def in_cooldown(changed_on: date | None, as_of: date | None, cooldown_months: int) -> bool:
    """True while ``as_of`` is before the end of the cooldown that started on ``changed_on``."""
    ends = cooldown_ends(changed_on, cooldown_months)
    return ends is not None and as_of is not None and as_of < ends


# --- The observation table ---------------------------------------------------------------------


def current_groups(session: Session, policy: ReverseConversionPolicy, oar_types: frozenset[str]) -> dict[Key, str]:
    """Every material-plant of the policy's plants whose MRP type puts it in the Min-Max or the OAR group."""
    min_max = frozenset(policy.mrp_types)
    wanted = sorted(min_max | oar_types)
    rows = session.execute(
        select(MaterialFeature.sap_material_number, MaterialFeature.sap_plant_code, MaterialFeature.mrp_type).where(
            MaterialFeature.sap_plant_code.in_(policy.plants), MaterialFeature.mrp_type.in_(wanted)
        )
    )
    groups: dict[Key, str] = {}
    for material, plant, mrp in rows:
        group = planning_group(mrp, min_max, oar_types)
        if group is not None:
            groups[(material, plant)] = group
    return groups


def load_observations(session: Session) -> dict[Key, Observation]:
    return {
        (row.sap_material_number, row.sap_plant_code): Observation(row.planning_group, row.first_seen_on, row.changed_on)
        for row in session.scalars(select(PlanningGroupObservation))
    }


def record_observations(session: Session, policy: ReverseConversionPolicy, oar_types: frozenset[str], today: date) -> ObservationChanges:
    """Bring the observation table up to date with today's groups. Flushes; the caller commits."""
    changes = observe(load_observations(session), current_groups(session, policy, oar_types), today)
    session.add_all(
        PlanningGroupObservation(
            sap_material_number=m, sap_plant_code=p, planning_group=o.group, first_seen_on=o.first_seen_on, changed_on=o.changed_on
        )
        for (m, p), o in changes.new.items()
    )
    for (m, p), o in changes.changed.items():
        row = session.scalars(
            select(PlanningGroupObservation).where(
                PlanningGroupObservation.sap_material_number == m, PlanningGroupObservation.sap_plant_code == p
            )
        ).one()
        row.planning_group = o.group
        row.changed_on = o.changed_on
    session.flush()
    return changes


def changed_days(session: Session) -> dict[Key, date]:
    """``changed_on`` of every material-plant that has been seen to change group."""
    return {
        (row.sap_material_number, row.sap_plant_code): row.changed_on
        for row in session.scalars(select(PlanningGroupObservation).where(PlanningGroupObservation.changed_on.is_not(None)))
    }
