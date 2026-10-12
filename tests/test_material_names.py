"""Material names for the OAR screens -- MAKT first, then ZMM065.

``load_all`` runs its real queries against in-memory SQLite. The request side
(``material_names``) is tested for the property that matters in production: it
never waits on the database, and names appear once the background load ends.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.shared import material_names as names


def _engine(*, makt: bool = True, zmm: bool = True):
    # One shared connection: an in-memory database is private to the
    # connection that made it, and the load runs on another thread.
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
    return engine


@pytest.fixture
def database(monkeypatch):
    """Point the background load at an in-memory database."""
    engine = _engine()
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: sessionmaker(bind=engine))
    names.clear_cache()
    yield engine
    names.clear_cache()


def _loaded() -> dict[str, str]:
    found, _ = names.load_all(Session(_engine()))
    return found


def test_prefers_english_makt_text():
    assert _loaded()["8000005632"] == "SLURRY PUMP IMPELLER"


def test_takes_another_language_when_makt_has_no_english():
    assert _loaded()["8000000001"] == "LAGER"


def test_falls_back_to_zmm065_and_normalises_its_padded_number():
    assert _loaded()["8000000002"] == "IDLER BEARING"


def test_a_missing_table_is_skipped_and_reported():
    found, complete = names.load_all(Session(_engine(makt=False)))
    assert complete is False
    assert found == {"8000000002": "IDLER BEARING", "8000005632": "IGNORED -- MAKT ALREADY NAMES IT"}


def test_a_request_never_waits_for_the_load(database, monkeypatch):
    started = []
    monkeypatch.setattr(names, "_ensure_loading", lambda: started.append(True))
    # Nothing loaded yet: no names, and no query on the request's own path.
    assert names.material_names(["8000005632"]) == {}
    assert started == [True]


def test_names_appear_once_the_background_load_ends(database):
    assert names.material_names(["000000008000005632"]) in ({}, {"8000005632": "SLURRY PUMP IMPELLER"})
    names.wait_for_load(5)
    assert names.material_names(["000000008000005632", "123", None, ""]) == {
        "8000005632": "SLURRY PUMP IMPELLER"
    }


def test_attach_sets_the_field_on_each_row(database):
    names.material_names([])
    names.wait_for_load(5)
    rows = [SimpleNamespace(material="8000005632"), SimpleNamespace(material="123")]
    names.attach_material_names(rows)
    assert [r.material_description for r in rows] == ["SLURRY PUMP IMPELLER", None]


def test_the_route_decorator_keeps_the_query_parameters_and_fills_names(database):
    from fastapi import FastAPI, Query
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from app.api.i13.deps import with_material_names

    class Row(BaseModel):
        material: str
        material_description: str | None = None

    class Wrapped(BaseModel):
        exception: Row

    api = FastAPI()

    @api.get("/rows", response_model=list[Row])
    @with_material_names()
    def rows(plant: str | None = Query(None)) -> list[Row]:
        return [Row(material="8000005632"), Row(material="123")]

    @api.get("/wrapped", response_model=list[Wrapped])
    @with_material_names(lambda row: row.exception)
    def wrapped() -> list[Wrapped]:
        return [Wrapped(exception=Row(material="8000000002"))]

    names.material_names([])
    names.wait_for_load(5)
    client = TestClient(api)
    assert [r["material_description"] for r in client.get("/rows").json()] == ["SLURRY PUMP IMPELLER", None]
    assert client.get("/wrapped").json()[0]["exception"]["material_description"] == "IDLER BEARING"
    params = client.get("/openapi.json").json()["paths"]["/rows"]["get"]["parameters"]
    assert [p["name"] for p in params] == ["plant"]
