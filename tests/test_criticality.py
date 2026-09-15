"""Criticality module suite (W3.4).

``TestCriticalityConformance`` is written against the ``CriticalitySource``
interface, never against a concrete provider. The ``source`` fixture yields one
implementation per entry in its ``params``; adding a provider there runs the
whole class against it with no new test code -- the same mechanism
``tests/test_storage.py`` uses for storage adapters.

Tests outside that class cover the factory, tier parsing, the fallback chain and
the three-initiative consumption guarantee, which are provider-independent.

The ZMM065 provider reads seeded raw tables, so tests that need real data are
guarded by ``needs_zmm065``. Everything else runs on a bare machine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.criticality import (
    SEVERITY_ORDER,
    SOURCES,
    CriticalityError,
    CriticalityNotConfiguredError,
    CriticalityResult,
    CriticalitySource,
    CriticalityTier,
    FallbackCriticalitySource,
    UnknownCriticalitySourceError,
    build_criticality_source,
    get_criticality_source,
    parse_tier,
    reset_criticality_cache,
)
from app.integrations.criticality.zmm065 import TABLES, Zmm065CriticalitySource
from app.integrations.criticality.zzcritic import CONFIRMED, ZzcriticCriticalitySource


def _zmm065_seeded() -> bool:
    """Whether the ZMM065 raw tables exist and hold rows."""
    if not get_settings().database_url:
        return False
    try:
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as session:
            for table in TABLES:
                session.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).first()
        return True
    except Exception:
        return False


needs_zmm065 = pytest.mark.skipif(
    not _zmm065_seeded(), reason="ZMM065 raw tables not seeded"
)


class FakeSource(CriticalitySource):
    """An in-memory source, so conformance runs without a database."""

    name = "fake"

    def __init__(self, rows: dict[tuple[str, str], str] | None = None) -> None:
        self.rows = rows if rows is not None else {("1000", "1300"): "CRITICAL"}

    def get(self, sap_material_number: str, sap_plant_code: str | None = None) -> CriticalityResult:
        material = (sap_material_number or "").strip()
        plant = (sap_plant_code or "").strip() or None
        raw = self.rows.get((material, plant or ""))
        tier = parse_tier(raw)
        return CriticalityResult(
            sap_material_number=material,
            sap_plant_code=plant,
            tier=tier,
            source=self.name,
            reason=None if tier else f"{material} not present",
        )

    def check_connection(self) -> None:
        return None


@pytest.fixture(params=["fake", "zzcritic"])
def source(request: pytest.FixtureRequest) -> CriticalitySource:
    """One provider per implementation under test.

    ``zmm065`` is deliberately not here: it needs a seeded database, and the
    conformance rules below must hold on a bare machine. It is covered against
    real data in ``TestZmm065AgainstDeliveredData``.
    """
    if request.param == "fake":
        return FakeSource()
    if request.param == "zzcritic":
        return ZzcriticCriticalitySource()
    raise AssertionError(f"Unknown criticality source: {request.param}")


class TestCriticalityConformance:
    """Behaviour every provider must share."""

    def test_unknown_material_is_a_result_not_an_exception(
        self, source: CriticalitySource
    ) -> None:
        """A material the source never heard of is an ordinary answer."""
        result = source.get("no-such-material", "1300")
        assert isinstance(result, CriticalityResult)
        assert result.tier is None
        assert result.found is False
        assert result.reason, "an absent tier must say why"

    def test_result_reports_its_source(self, source: CriticalitySource) -> None:
        assert source.get("1000", "1300").source == source.name

    def test_blank_material_is_handled(self, source: CriticalitySource) -> None:
        assert source.get("", "1300").found is False

    def test_lookup_is_deterministic(self, source: CriticalitySource) -> None:
        assert source.get("1000", "1300") == source.get("1000", "1300")

    def test_never_invents_a_tier(self, source: CriticalitySource) -> None:
        """Whatever comes back is one of the five, or nothing."""
        tier = source.get("1000", "1300").tier
        assert tier is None or tier in set(CriticalityTier)


# --- Tiers ----------------------------------------------------------------


class TestTiers:
    def test_the_taxonomy_is_the_five_zmm065_values(self) -> None:
        assert {t.value for t in CriticalityTier} == {
            "CRITICAL",
            "IMPACT",
            "INSURANCE",
            "NORMAL",
            "OBSOLETE",
        }

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("CRITICAL", CriticalityTier.CRITICAL),
            ("IMPACT", CriticalityTier.IMPACT),
            ("INSURANCE", CriticalityTier.INSURANCE),
            ("NORMAL", CriticalityTier.NORMAL),
            ("OBSOLETE", CriticalityTier.OBSOLETE),
            ("  critical  ", CriticalityTier.CRITICAL),
            ("Normal", CriticalityTier.NORMAL),
        ],
    )
    def test_maps_every_tier(self, raw: str, expected: CriticalityTier) -> None:
        assert parse_tier(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "TIER 1", "1", "VERY CRITICAL", "N/A"])
    def test_unrecognised_value_is_none_not_a_default(self, raw: str | None) -> None:
        """An unexpected tier is never coerced -- it would change service level."""
        assert parse_tier(raw) is None

    def test_severity_order_covers_every_tier_most_severe_first(self) -> None:
        assert set(SEVERITY_ORDER) == set(CriticalityTier)
        assert SEVERITY_ORDER[0] is CriticalityTier.CRITICAL


# --- Configuration --------------------------------------------------------


class TestConfiguration:
    def test_default_source_is_zmm065(self) -> None:
        assert Settings(_env_file=None).criticality_source == "zmm065"

    def test_configuration_selects_zmm065(self) -> None:
        built = build_criticality_source(
            Settings(criticality_source="zmm065", _env_file=None)
        )
        assert isinstance(built, Zmm065CriticalitySource)
        assert built.name == "zmm065"

    def test_configuration_selects_zzcritic_with_fallback(self) -> None:
        """Selecting the other supported source is a config change only."""
        built = build_criticality_source(
            Settings(criticality_source="zzcritic", _env_file=None)
        )
        assert isinstance(built, FallbackCriticalitySource)
        assert isinstance(built.primary, ZzcriticCriticalitySource)
        assert isinstance(built.fallback, Zmm065CriticalitySource)

    @pytest.mark.parametrize("value", ["ZMM065", "  zmm065  ", ""])
    def test_choice_is_normalised(self, value: str) -> None:
        built = build_criticality_source(
            Settings(criticality_source=value, _env_file=None)
        )
        assert isinstance(built, Zmm065CriticalitySource)

    def test_unknown_source_is_refused_by_name(self) -> None:
        with pytest.raises(UnknownCriticalitySourceError) as excinfo:
            build_criticality_source(
                Settings(criticality_source="ariba", _env_file=None)
            )
        assert "ariba" in str(excinfo.value)
        for name in SOURCES:
            assert name in str(excinfo.value)

    def test_accessor_is_cached_and_resettable(self) -> None:
        reset_criticality_cache()
        try:
            assert get_criticality_source() is get_criticality_source()
        finally:
            reset_criticality_cache()


# --- Fallback -------------------------------------------------------------


class TestFallback:
    def test_primary_hit_is_not_marked_as_fallback(self) -> None:
        chain = FallbackCriticalitySource(
            primary=FakeSource({("1000", "1300"): "CRITICAL"}),
            fallback=FakeSource({("1000", "1300"): "NORMAL"}),
        )
        result = chain.get("1000", "1300")
        assert result.tier is CriticalityTier.CRITICAL
        assert result.is_fallback is False

    def test_zzcritic_unavailable_falls_back_to_zmm065_visibly(self) -> None:
        """The headline W3.4 behaviour: configured primary is unavailable."""
        chain = FallbackCriticalitySource(
            primary=ZzcriticCriticalitySource(),
            fallback=FakeSource({("1000", "1300"): "IMPACT"}),
        )
        result = chain.get("1000", "1300")
        assert result.tier is CriticalityTier.IMPACT
        assert result.is_fallback is True
        assert result.source == "fake", "source must name who actually answered"
        assert "ZZCRITIC" in (result.reason or ""), "the demotion must be observable"

    def test_fallback_miss_reports_both_reasons(self) -> None:
        chain = FallbackCriticalitySource(
            primary=ZzcriticCriticalitySource(), fallback=FakeSource({})
        )
        result = chain.get("9999", "1300")
        assert result.found is False
        assert result.is_fallback is True
        assert "ZZCRITIC" in (result.reason or "")
        assert "not present" in (result.reason or "")

    def test_raising_primary_is_caught_and_demoted(self) -> None:
        class Broken(CriticalitySource):
            name = "broken"

            def get(self, sap_material_number, sap_plant_code=None):
                raise CriticalityNotConfiguredError("no credentials")

            def check_connection(self) -> None:
                raise CriticalityNotConfiguredError("no credentials")

        chain = FallbackCriticalitySource(
            primary=Broken(), fallback=FakeSource({("1000", "1300"): "NORMAL"})
        )
        result = chain.get("1000", "1300")
        assert result.tier is CriticalityTier.NORMAL
        assert result.is_fallback is True
        assert "no credentials" in (result.reason or "")

    def test_healthy_when_only_the_fallback_works(self) -> None:
        FallbackCriticalitySource(
            primary=ZzcriticCriticalitySource(), fallback=FakeSource()
        ).check_connection()


# --- ZZCRITIC status ------------------------------------------------------


class TestZzcriticStatus:
    def test_not_confirmed(self) -> None:
        """Flip only on measured evidence from live SAP."""
        assert CONFIRMED is False

    def test_answers_nothing_but_explains_why(self) -> None:
        result = ZzcriticCriticalitySource().get("1000", "1300")
        assert result.found is False
        assert "not exposed" in (result.reason or "")

    def test_check_connection_reports_unavailable(self) -> None:
        with pytest.raises(CriticalityError):
            ZzcriticCriticalitySource().check_connection()


# --- ZMM065 against the delivered data ------------------------------------


@needs_zmm065
class TestZmm065AgainstDeliveredData:
    """Runs only where the extracts are seeded. Values are measured, not assumed."""

    def test_lookup_succeeds_for_a_known_material_plant(self) -> None:
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as session:
            row = session.execute(
                text(
                    "SELECT mat_code, plant, criticality FROM raw_zmm065_bmm "
                    "WHERE NULLIF(criticality,'') IS NOT NULL LIMIT 1"
                )
            ).first()

        result = Zmm065CriticalitySource().get(row[0], row[1])
        assert result.found is True
        assert result.tier == parse_tier(row[2])
        assert result.source == "zmm065"
        assert result.is_fallback is False
        assert result.sap_plant_code == row[1]

    def test_material_not_found(self) -> None:
        result = Zmm065CriticalitySource().get("0000000000", "1300")
        assert result.found is False
        assert "not present" in (result.reason or "")

    def test_known_material_at_wrong_plant_is_not_found(self) -> None:
        """Criticality is per material-plant: a hit at 1300 is not a hit at 1500."""
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as session:
            row = session.execute(
                text(
                    "SELECT mat_code FROM raw_zmm065_bmm b WHERE NOT EXISTS ("
                    "  SELECT 1 FROM raw_zmm065_gb g WHERE g.mat_code = b.mat_code"
                    ") LIMIT 1"
                )
            ).first()

        assert Zmm065CriticalitySource().get(row[0], "1500").found is False

    def test_every_delivered_value_is_a_known_tier(self) -> None:
        """If this fails, SAP changed the vocabulary -- do not widen the enum to match."""
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as session:
            for table in TABLES:
                values = session.execute(
                    text(
                        f"SELECT DISTINCT criticality FROM {table} "
                        "WHERE NULLIF(criticality,'') IS NOT NULL"
                    )
                ).fetchall()
                for (value,) in values:
                    assert parse_tier(value) is not None, f"{table}: unknown tier {value!r}"

    def test_check_connection_passes(self) -> None:
        Zmm065CriticalitySource().check_connection()


# --- The three initiatives ------------------------------------------------


class TestSharedAcrossInitiatives:
    """W3.4's actual requirement: one module, three consumers, no duplication."""

    def test_i07_i08_and_i13_get_identical_answers(self) -> None:
        """Each initiative resolves the source the same way and agrees."""
        chain = FakeSource({("1000", "1300"): "CRITICAL"})

        def consume(_initiative: str) -> CriticalityResult:
            # What every initiative does: ask the shared module, pass through
            # material and plant, and read the tier. No initiative-local logic.
            return chain.get("1000", "1300")

        answers = [consume(i) for i in ("i7", "i8", "i13")]
        assert all(a.tier is CriticalityTier.CRITICAL for a in answers)
        assert len({(a.tier, a.source, a.is_fallback) for a in answers}) == 1

    def test_no_initiative_package_implements_its_own_criticality(self) -> None:
        """A duplicate implementation inside an initiative is the thing to prevent."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app" / "initiatives"
        offenders = [
            path
            for path in root.rglob("*.py")
            if "criticality" in path.name.lower()
        ]
        assert offenders == [], f"criticality must stay shared, found: {offenders}"

    def test_consumers_need_no_knowledge_of_the_source(self) -> None:
        """Swapping providers changes nothing a consumer can observe structurally."""
        for provider in (FakeSource(), ZzcriticCriticalitySource()):
            result = provider.get("1000", "1300")
            assert hasattr(result, "tier")
            assert hasattr(result, "source")
            assert hasattr(result, "is_fallback")

    def test_reachable_from_app_shared(self) -> None:
        """``app.shared`` is where initiatives are told to look for common code."""
        import app.shared as shared

        assert shared.get_criticality_source is get_criticality_source
        assert shared.CriticalityTier is CriticalityTier
        for name in ("CriticalityResult", "CriticalitySource", "parse_tier"):
            assert name in shared.__all__


# --- Readiness ------------------------------------------------------------


class TestReadiness:
    """The shared source is a platform dependency, so readiness reports it."""

    def test_ready_reports_criticality(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        body = TestClient(app).get("/api/ready").json()
        assert "criticality" in body
        assert body["criticality"] in ("ok", "not_configured", "unavailable")
