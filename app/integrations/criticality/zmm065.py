"""Criticality from the delivered ZMM065 aging reports -- the default source.

ZMM065 arrives as two workbooks, one per site, already loaded into the raw layer
by ``app.seed``:

    raw_zmm065_bmm   Black Mountain, plant 1300, 10,521 rows
    raw_zmm065_gb    Gamsberg,       plant 1500,  7,449 rows

Both carry ``mat_code``, ``plant`` and ``criticality``, and both are indexed on
``mat_code`` and ``plant`` by the seed loader, so a lookup is an index hit rather
than a scan over 18k rows.

**The two tables are unioned, then filtered by plant -- never collapsed to one
row per material.** 561 materials appear in both deliveries, and an aggregate
over them would have to pick a winner. There is no correct winner: a material
genuinely can be CRITICAL at Gamsberg and NORMAL at Black Mountain, and both
statements are true at their own plant. Callers therefore pass the plant, and a
lookup without one is answered only when the material is unambiguous.

Every value stays text through the raw layer on purpose (see ``app.seed.reader``),
so the mapping to a tier happens here, in one place, via ``parse_tier``.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.criticality import (
    CriticalityNotConfiguredError,
    CriticalityResult,
    CriticalitySource,
    parse_tier,
)
from app.core.db import DatabaseNotConfiguredError, get_sessionmaker
from app.core.logging import get_logger

logger = get_logger(__name__)

SOURCE_NAME = "zmm065"

#: The delivered tables, in the order the seed manifest declares them.
TABLES = ("raw_zmm065_bmm", "raw_zmm065_gb")

# One row per material-plant. NULLIF drops blank criticality so a row that
# carries no tier reads as absent rather than as an empty-string tier.
#
# Table names are interpolated from the module-level TABLES constant, never from
# input -- a raw table name cannot be a bind parameter, and the only values that
# reach this string are the two literals above.
_UNION = " UNION ALL ".join(
    f"SELECT mat_code, plant, criticality FROM {table}" for table in TABLES
)

_BY_MATERIAL_PLANT = text(
    f"""
    SELECT criticality
      FROM ({_UNION}) z
     WHERE z.mat_code = :material
       AND z.plant = :plant
       AND NULLIF(z.criticality, '') IS NOT NULL
    """
)

# Without a plant, every distinct tier the material carries is returned so the
# caller can be told *why* an answer is being withheld rather than getting one
# plant's tier at random.
_BY_MATERIAL = text(
    f"""
    SELECT DISTINCT criticality
      FROM ({_UNION}) z
     WHERE z.mat_code = :material
       AND NULLIF(z.criticality, '') IS NOT NULL
    """
)

# Proves the tables exist without reading a row -- portable, unlike LIMIT.
_PROBE = text(f"SELECT 1 FROM ({_UNION}) z WHERE 1 = 0")

# Every row for a batch of materials, in one round trip. The plant is NOT
# filtered here: one pass over the requested materials answers both the
# per-plant keys and the any-plant keys, and the grouping happens in Python
# where the ambiguity rule already lives.
_MANY = text(
    f"""
    SELECT z.mat_code, z.plant, z.criticality
      FROM ({_UNION}) z
     WHERE z.mat_code IN :materials
       AND NULLIF(z.criticality, '') IS NOT NULL
    """
).bindparams(bindparam("materials", expanding=True))

#: Materials per _MANY round trip. Each is one bind parameter, and SQL Server
#: refuses a statement with more than 2,100.
_MANY_CHUNK = 1000


class Zmm065CriticalitySource(CriticalitySource):
    """Reads the seeded ZMM065 raw tables. Deterministic and read-only."""

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

        with self._session() as session:
            if plant is not None:
                row = session.execute(
                    _BY_MATERIAL_PLANT, {"material": material, "plant": plant}
                ).first()
                raw = row[0] if row else None
                reason = None if row else f"{material} not present at plant {plant} in ZMM065"
            else:
                rows = session.execute(_BY_MATERIAL, {"material": material}).fetchall()
                tiers = {r[0] for r in rows}
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
            {(key[0] or "").strip() for key in wanted if (key[0] or "").strip()}
        )

        # by_material_plant answers the keys that name a plant; by_material
        # collects every distinct tier a material carries, which is what the
        # plant-less ambiguity rule needs.
        by_material_plant: dict[tuple[str, str], str] = {}
        by_material: dict[str, set[str]] = {}
        if materials:
            with self._session() as session:
                rows = [
                    row
                    for start in range(0, len(materials), _MANY_CHUNK)
                    for row in session.execute(
                        _MANY, {"materials": materials[start : start + _MANY_CHUNK]}
                    ).fetchall()
                ]
            for mat_code, plant, criticality in rows:
                material = (mat_code or "").strip()
                plant_code = (plant or "").strip() or None
                if plant_code is not None:
                    # .first() in the single-key query means first row wins; the
                    # same rule here, via setdefault.
                    by_material_plant.setdefault((material, plant_code), criticality)
                by_material.setdefault(material, set()).add(criticality)

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

            if plant is not None:
                raw = by_material_plant.get((material, plant))
                reason = (
                    None
                    if raw is not None
                    else f"{material} not present at plant {plant} in ZMM065"
                )
            else:
                tiers = by_material.get(material, set())
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
        """Prove both tables exist and are readable."""
        with self._session() as session:
            session.execute(_PROBE)

    def _session(self):
        try:
            return get_sessionmaker()()
        except DatabaseNotConfiguredError as exc:
            raise CriticalityNotConfiguredError(
                "ZMM065 criticality needs the database that holds the seeded raw "
                "tables. Set DATABASE_URL and run `python -m app.seed --all`."
            ) from exc
        except SQLAlchemyError as exc:  # pragma: no cover - driver-level failure
            raise CriticalityNotConfiguredError(f"ZMM065 source unusable: {exc}") from exc
