"""Material names for the OAR screens -- MAKT first, then ZMM065.

Runs the real queries against in-memory SQLite: the lookup is plain SELECTs
with an expanding IN list, which SQLite answers the way SQL Server does.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.shared import material_names as names


@pytest.fixture(autouse=True)
def _fresh_cache():
    names.clear_cache()
    yield
    names.clear_cache()


def _session(*, makt: bool = True, zmm: bool = True) -> Session:
    # One shared connection: FastAPI runs a sync route on a worker thread, and
    # an in-memory database is private to the connection that made it.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as c:
        if makt:
            c.execute(text("CREATE TABLE n_makt (material TEXT, language_key TEXT, material_description TEXT)"))
            c.execute(
                text("INSERT INTO n_makt VALUES (:m, :l, :d)"),
                [
                    {"m": "8000005632", "l": "D", "d": "PUMPE"},
                    {"m": "8000005632", "l": "E", "d": "SLURRY PUMP IMPELLER"},
                    {"m": "8000000001", "l": "D", "d": "LAGER"},
                    {"m": "8000000002", "l": "E", "d": ""},
                ],
            )
        if zmm:
            c.execute(text("CREATE TABLE raw_zmm065_bmm (mat_code TEXT, plant TEXT, material_description TEXT)"))
            c.execute(
                text("INSERT INTO raw_zmm065_bmm VALUES (:m, '1300', :d)"),
                [
                    {"m": "000000008000000002", "d": "IDLER BEARING"},
                    {"m": "8000005632", "d": "IGNORED -- MAKT ALREADY NAMES IT"},
                ],
            )
    return Session(engine)


def test_prefers_english_makt_text():
    assert names.material_names(_session(), ["8000005632"]) == {"8000005632": "SLURRY PUMP IMPELLER"}


def test_takes_another_language_when_makt_has_no_english():
    assert names.material_names(_session(), ["8000000001"]) == {"8000000001": "LAGER"}


def test_falls_back_to_zmm065_and_matches_a_padded_number():
    assert names.material_names(_session(), ["8000000002"]) == {"8000000002": "IDLER BEARING"}


def test_normalises_the_numbers_it_is_asked_for():
    assert names.material_names(_session(), ["000000008000005632"]) == {"8000005632": "SLURRY PUMP IMPELLER"}


def test_leaves_out_a_material_nobody_names():
    assert names.material_names(_session(), ["123", None, ""]) == {}


def test_a_missing_table_is_skipped_not_fatal():
    assert names.material_names(_session(makt=False), ["8000000002", "8000005632"]) == {
        "8000000002": "IDLER BEARING",
        "8000005632": "IGNORED -- MAKT ALREADY NAMES IT",
    }


def test_caches_names_but_not_misses_from_an_unreadable_source():
    names.material_names(_session(makt=False), ["8000000001"])
    # MAKT could not be read, so the miss was not remembered: a later call that
    # can read it finds the name.
    assert names.material_names(_session(), ["8000000001"]) == {"8000000001": "LAGER"}
    # And a found name is served from the cache, even from an empty database.
    assert names.material_names(_session(makt=False, zmm=False), ["8000000001"]) == {"8000000001": "LAGER"}


def test_attach_sets_the_field_on_each_row():
    rows = [SimpleNamespace(material="8000005632"), SimpleNamespace(material="123")]
    names.attach_material_names(_session(), rows)
    assert [r.material_description for r in rows] == ["SLURRY PUMP IMPELLER", None]


def test_the_route_decorator_keeps_the_query_parameters_and_fills_names():
    from fastapi import Depends, FastAPI, Query
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from app.api.i13.deps import with_material_names

    class Row(BaseModel):
        material: str
        material_description: str | None = None

    class Wrapped(BaseModel):
        exception: Row

    db = _session()
    api = FastAPI()

    @api.get("/rows", response_model=list[Row])
    @with_material_names()
    def rows(plant: str | None = Query(None), db: Session = Depends(lambda: db)) -> list[Row]:
        return [Row(material="8000005632"), Row(material="123")]

    @api.get("/wrapped", response_model=list[Wrapped])
    @with_material_names(lambda row: row.exception)
    def wrapped(db: Session = Depends(lambda: db)) -> list[Wrapped]:
        return [Wrapped(exception=Row(material="8000000002"))]

    client = TestClient(api)
    assert [r["material_description"] for r in client.get("/rows").json()] == ["SLURRY PUMP IMPELLER", None]
    assert client.get("/wrapped").json()[0]["exception"]["material_description"] == "IDLER BEARING"
    params = client.get("/openapi.json").json()["paths"]["/rows"]["get"]["parameters"]
    assert [p["name"] for p in params] == ["plant"]
