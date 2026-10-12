"""Criticality from the monthly ZMM065 reports -- the default source.

ZMM065 is each plant's criticality report, reissued every month. Reports are
uploaded on the Inventory Planning "Criticality reports" page (and the July
delivery imported as the first one), and ``criticality_current`` holds what
they add up to: one row per material-plant, the newest non-blank tier any
effective report gave (``app.integrations.criticality.reports``). This source
reads that table, so a new month's upload changes the answer for I07, I08 and
I13 together, with nothing in any of them changing.

**Per material-plant, never collapsed to one row per material.** 561 materials
appear in both plants, and an aggregate over them would have to pick a winner.
There is no correct winner: a material genuinely can be CRITICAL at Gamsberg
and NORMAL at Black Mountain, and both statements are true at their own plant.
Callers therefore pass the plant, and a lookup without one is answered only
when the material is unambiguous.

**The material is compared in the stored form** -- leading zeros stripped,
trimmed -- so a padded and an unpadded number find the same row.

**Not loaded is not an error.** Before the first report is imported (or before
the migration has run) every answer is "no tier" with the reason ``ZMM065 not
loaded`` -- never a raised error, so the builds that ask (I13 reclassification,
I08) carry on with criticality unknown -- and ``check_connection`` reports
``CriticalityNotLoadedError``.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.criticality import (
    CriticalityNotConfiguredError,
    CriticalityNotLoadedError,
    CriticalityResult,
    CriticalitySource,
    parse_tier,
)
from app.core.db import DatabaseNotConfiguredError, get_sessionmaker
from app.core.logging import get_logger
from app.integrations.criticality.report_parser import material_key
from app.models.criticality_report import CriticalityCurrent

logger = get_logger(__name__)

SOURCE_NAME = "zmm065"

NOT_LOADED = "ZMM065 not loaded"

#: Materials per round trip. Each is one bind parameter, and SQL Server
#: refuses a statement with more than 2,100.
_MANY_CHUNK = 1000


def _has_rows(session: Session) -> bool:
    """Whether any criticality is loaded. A table that does not exist yet (the
    migration has not run) reads as not loaded, not as a failure."""
    try:
        return session.execute(select(CriticalityCurrent.material).limit(1)).first() is not None
    except DBAPIError:
        session.rollback()
        return False


class Zmm065CriticalitySource(CriticalitySource):
    """Reads ``criticality_current``. Deterministic and read-only."""

    name = SOURCE_NAME

    def get(self, sap_material_number: str, sap_plant_code: str | None = None) -> CriticalityResult:
        material = (sap_material_number or "").strip()
        plant = (sap_plant_code or "").strip() or None

        if not material:
            return CriticalityResult(
                sap_material_number=sap_material_number,
                sap_plant_code=sap_plant_code,
                tier=None,
                source=self.name,
                reason="no material number given",
            )

        stored = material_key(material)
        with self._session() as session:
            if not _has_rows(session):
                raw, reason = None, NOT_LOADED
            elif plant is not None:
                row = session.execute(
                    select(CriticalityCurrent.criticality).where(
                        CriticalityCurrent.material == stored,
                        CriticalityCurrent.plant == plant,
                        CriticalityCurrent.criticality.is_not(None),
                    )
                ).first()
                raw = row[0] if row else None
                reason = None if row else f"{material} not present at plant {plant} in ZMM065"
            else:
                # Without a plant, every distinct tier the material carries is
                # returned so the caller can be told *why* an answer is being
                # withheld rather than getting one plant's tier at random.
                tiers = set(
                    session.scalars(
                        select(CriticalityCurrent.criticality)
                        .where(
                            CriticalityCurrent.material == stored,
                            CriticalityCurrent.criticality.is_not(None),
                        )
                        .distinct()
                    )
                )
                if not tiers:
                    raw, reason = None, f"{material} not present in ZMM065"
                elif len(tiers) == 1:
                    raw, reason = tiers.pop(), None
                else:
                    # Ambiguous across plants -- answering would mean guessing.
                    raw = None
                    reason = (
                        f"{material} carries differing tiers across plants "
                        f"({', '.join(sorted(tiers))}); pass a plant to resolve it"
                    )

        tier = parse_tier(raw)
        if raw is not None and tier is None:
            # A value that is present but not one of the five. Not coerced.
            reason = f"unrecognised ZMM065 criticality {raw!r}"
            logger.warning(
                "ZMM065 criticality %r for material %s is not a known tier; returning none.",
                raw,
                material,
            )

        return CriticalityResult(
            sap_material_number=material,
            sap_plant_code=plant,
            tier=tier,
            source=self.name,
            reason=reason,
        )

    def get_many(
        self, keys: Iterable[tuple[str, str | None]]
    ) -> dict[tuple[str, str | None], CriticalityResult]:
        """The whole batch in one query and one session.

        The base implementation is correct but opens a session per key, which is
        26 seconds over I08's 3,802-row universe. This reads every row for the
        requested materials once and groups them in Python, so the cost is one
        round trip regardless of how many keys are asked for.

        **The answers are identical to :meth:`get`, key for key** -- same plant
        precedence, same ambiguity rule, same reason strings -- and the shared
        conformance suite asserts exactly that. Only the number of queries
        differs.
        """
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}

        materials = sorted(
            {material_key(key[0]) for key in wanted if (key[0] or "").strip()}
        )

        # by_material_plant answers the keys that name a plant; by_material
        # collects every distinct tier a material carries, which is what the
        # plant-less ambiguity rule needs.
        by_material_plant: dict[tuple[str, str], str] = {}
        by_material: dict[str, set[str]] = {}
        loaded = True
        if materials:
            with self._session() as session:
                loaded = _has_rows(session)
                rows = (
                    [
                        row
                        for start in range(0, len(materials), _MANY_CHUNK)
                        for row in session.execute(
                            select(
                                CriticalityCurrent.material,
                                CriticalityCurrent.plant,
                                CriticalityCurrent.criticality,
                            ).where(
                                CriticalityCurrent.material.in_(materials[start : start + _MANY_CHUNK]),
                                CriticalityCurrent.criticality.is_not(None),
                            )
                        ).all()
                    ]
                    if loaded
                    else []
                )
            for stored, plant_code, criticality in rows:
                by_material_plant.setdefault((stored, plant_code), criticality)
                by_material.setdefault(stored, set()).add(criticality)

        results: dict[tuple[str, str | None], CriticalityResult] = {}
        for key in wanted:
            material = (key[0] or "").strip()
            plant = (key[1] or "").strip() or None

            if not material:
                results[key] = CriticalityResult(
                    sap_material_number=key[0],
                    sap_plant_code=key[1],
                    tier=None,
                    source=self.name,
                    reason="no material number given",
                )
                continue

            stored = material_key(material)
            if not loaded:
                raw, reason = None, NOT_LOADED
            elif plant is not None:
                raw = by_material_plant.get((stored, plant))
                reason = (
                    None
                    if raw is not None
                    else f"{material} not present at plant {plant} in ZMM065"
                )
            else:
                tiers = by_material.get(stored, set())
                if not tiers:
                    raw, reason = None, f"{material} not present in ZMM065"
                elif len(tiers) == 1:
                    raw, reason = next(iter(tiers)), None
                else:
                    raw = None
                    reason = (
                        f"{material} carries differing tiers across plants "
                        f"({', '.join(sorted(tiers))}); pass a plant to resolve it"
                    )

            tier = parse_tier(raw)
            if raw is not None and tier is None:
                reason = f"unrecognised ZMM065 criticality {raw!r}"

            results[key] = CriticalityResult(
                sap_material_number=material,
                sap_plant_code=plant,
                tier=tier,
                source=self.name,
                reason=reason,
            )
        return results

    def check_connection(self) -> None:
        """Prove the loaded reports are readable. None loaded is
        ``CriticalityNotLoadedError``: optional data, not an outage."""
        with self._session() as session:
            if not _has_rows(session):
                raise CriticalityNotLoadedError(
                    "No criticality report is loaded; criticality reads as unknown. Upload one on the "
                    "Inventory Planning 'Criticality reports' page, or import the July delivery with: "
                    "python -m app.integrations.criticality.baseline"
                )

    def _session(self):
        try:
            return get_sessionmaker()()
        except DatabaseNotConfiguredError as exc:
            raise CriticalityNotConfiguredError(
                "Criticality needs the database that holds the uploaded reports. Set DATABASE_URL."
            ) from exc
        except SQLAlchemyError as exc:  # pragma: no cover - driver-level failure
            raise CriticalityNotConfiguredError(f"ZMM065 source unusable: {exc}") from exc
