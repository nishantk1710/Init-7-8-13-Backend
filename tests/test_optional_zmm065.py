"""Criticality is optional: with nothing loaded, nothing fails.

ZMM065 is not part of the SAP CSV pull. On 10 Oct 2026 a database filled only
by that pull had no raw_zmm065_* tables, and that alone failed the I13 build,
every I08 view (the failed v_zmm065 took the rest of the script down) and the
readiness check. Criticality now lives in ``criticality_current`` (the monthly
reports), and the same promise holds for it: with the table missing or empty,
criticality reads as unknown, the views build (v_zmm065 empty), I07's material
extract runs, and readiness says "not_loaded" without failing. Needs a
database -- and it replaces ``criticality_current`` with a scratch copy, so run
it only against a scratch one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.config import get_settings

pytestmark = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

COLUMNS = ("material", "material_raw", "plant", "criticality", "description", "mattyp", "mrp_type")


@pytest.fixture()
def engine():
    from app.core.db import get_engine
    from app.shared import sap_normalise

    eng = get_engine()

    def drop() -> None:
        with eng.begin() as c:
            c.execute(text("DROP TABLE IF EXISTS criticality_current"))

    drop()
    sap_normalise.ensure_views(eng)
    yield eng
    drop()


def _load(engine, rows) -> None:
    with engine.begin() as c:
        c.execute(text(f"CREATE TABLE criticality_current ({', '.join(f'{x} NVARCHAR(200)' for x in COLUMNS)})"))
        for material, plant, tier in rows:
            c.execute(
                text(
                    f"INSERT INTO criticality_current ({', '.join(COLUMNS)}) "
                    "VALUES (:m, :m, :p, :t, '', '', '')"
                ),
                {"m": material, "p": plant, "t": tier},
            )


def test_criticality_reads_unknown_and_readiness_says_not_loaded(engine) -> None:
    from app.api.ready import NOT_LOADED, _check_criticality
    from app.core.criticality import CriticalityNotLoadedError
    from app.integrations.criticality.zmm065 import NOT_LOADED as REASON
    from app.integrations.criticality.zmm065 import Zmm065CriticalitySource

    source = Zmm065CriticalitySource()
    one = source.get("1000000001", "1300")
    assert one.tier is None and one.reason == REASON
    many = source.get_many([("1000000001", "1300"), ("1000000002", None)])
    assert all(r.tier is None and r.reason == REASON for r in many.values())
    with pytest.raises(CriticalityNotLoadedError):
        source.check_connection()
    state, detail = _check_criticality()
    assert state == NOT_LOADED and "not loaded" in detail


def test_a_loaded_report_is_read(engine) -> None:
    from app.integrations.criticality.zmm065 import Zmm065CriticalitySource

    _load(engine, [("1000000001", "1300", "CRITICAL")])
    source = Zmm065CriticalitySource()
    assert source.get("1000000001", "1300").tier is not None
    assert source.get_many([("1000000001", "1300")])[("1000000001", "1300")].tier is not None
    source.check_connection()  # raises nothing


@pytest.mark.parametrize("loaded", [False, True], ids=["not-loaded", "loaded"])
def test_the_i08_views_build(engine, loaded) -> None:
    from app.initiatives.i8 import views

    if loaded:
        _load(engine, [("1000000001", "1300", "CRITICAL")])
    built = views.ensure_views(engine)
    assert "v_zmm065" in built and "v_ekpo" in built
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM v_zmm065")).scalar() == (1 if loaded else 0)
        c.execute(text("SELECT TOP 1 * FROM v_ekpo")).fetchall()


def test_the_i07_material_extract_runs(engine) -> None:
    from app.core.db import get_sessionmaker
    from app.initiatives.i7.adapters.extract import _material_sql

    session = get_sessionmaker()()
    try:
        session.execute(text(_material_sql(session))).fetchall()
        _load(engine, [("1000000001", "1300", "CRITICAL")])
        session.execute(text(_material_sql(session))).fetchall()
    finally:
        session.close()
