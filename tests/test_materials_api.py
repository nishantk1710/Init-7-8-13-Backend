"""More -> Materials: every active material + plant with its stock on hand.

The real reads (MARC scope index, MARD current stock, MARA unit) run against
in-memory SQLite tables shaped like ``n_marc`` / ``n_mard`` / ``n_mara``; the
material-name list is pinned, since it loads in the background.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.shared import materials_list

MARC = [
    # material, plant, DISMM, MARC-LVORM
    ("1000000001", "1300", "ND", ""),  # OAR, stock in two storage locations
    ("1000000001", "1500", "PD", ""),  # OAR, no MARD row at all
    ("1000000002", "1300", "VB", ""),  # Non-OAR (reorder point)
    ("1000000003", "1300", "", None),  # Non-OAR (blank DISMM), stock 0 in MARD
    ("1000000004", "1300", "ND", "X"),  # deleted at this plant
    ("1000000005", "1300", "PD", ""),  # deleted at client level (MARA)
    ("1000000005", "1500", "VB", ""),  # ... so at every plant
    ("1000000006", "9999", "ND", ""),  # out-of-scope plant
]
MARA = [
    # material, MARA-LVORM, MEINS
    ("1000000001", "", "EA"),
    ("1000000002", None, "KG"),
    ("1000000003", "", ""),
    ("1000000004", "", "EA"),
    ("1000000005", "X", "EA"),
    ("1000000006", "", "EA"),
]
MARD = [
    # material, plant, storage location, LABST
    ("1000000001", "1300", "1001", "10.000"),
    ("1000000001", "1300", "1002", "5.500"),
    ("1000000002", "1300", "1001", "3"),
    ("1000000003", "1300", "1001", "0"),
    ("1000000004", "1300", "1001", "99"),
]
NAMES = {
    "1000000001": "BEARING, BALL 6205",
    "1000000002": "GREASE, LITHIUM EP2",
    "1000000003": "BOLT, HEX M12",
}


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE n_marc (material TEXT, plant TEXT, mrp_type TEXT, mrp_controller TEXT, "
                "df_at_plant_level TEXT)"
            )
        )
        c.execute(text("CREATE TABLE n_mara (material TEXT, df_at_client_level TEXT, base_unit_of_measure TEXT)"))
        c.execute(text("CREATE TABLE n_mard (material TEXT, plant TEXT, storage_location TEXT, unrestricted TEXT)"))
        c.execute(
            text("INSERT INTO n_marc VALUES (:m, :p, :t, 'X01', :d)"), [dict(zip("mptd", r)) for r in MARC]
        )
        c.execute(text("INSERT INTO n_mara VALUES (:m, :d, :u)"), [dict(zip("mdu", r)) for r in MARA])
        c.execute(text("INSERT INTO n_mard VALUES (:m, :p, :l, :q)"), [dict(zip("mplq", r)) for r in MARD])

    def db():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(
        materials_list, "material_names", lambda materials: {m: NAMES[m] for m in materials if m in NAMES}
    )
    materials_list.clear_cache()
    app.dependency_overrides[get_db] = db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    materials_list.clear_cache()


def keys(body) -> list[tuple[str, str]]:
    return [(row["material"], row["plant"]) for row in body]


def test_active_in_scope_pairs_only(client):
    response = client.get("/api/materials", params={"limit": 100})
    assert response.status_code == 200
    assert keys(response.json()) == [
        ("1000000001", "1300"),
        ("1000000001", "1500"),
        ("1000000002", "1300"),
        ("1000000003", "1300"),
    ]
    assert response.headers["X-Total-Count"] == "4"


def test_stock_is_summed_over_storage_locations_and_missing_mard_is_zero(client):
    rows = {(r["material"], r["plant"]): r for r in client.get("/api/materials").json()}
    assert rows[("1000000001", "1300")]["stock_on_hand"] == "15.500"
    assert rows[("1000000001", "1500")]["stock_on_hand"] == "0"
    assert rows[("1000000003", "1300")]["stock_on_hand"] == "0"


def test_row_carries_description_plant_name_classification_and_unit(client):
    row = client.get("/api/materials").json()[0]
    assert row == {
        "material": "1000000001",
        "material_description": "BEARING, BALL 6205",
        "plant": "1300",
        "plant_name": "Black Mountain Mining",
        "classification": "OAR",
        "stock_on_hand": "15.500",
        "unit": "EA",
    }


def test_blank_unit_and_unknown_name_are_none(client):
    rows = {(r["material"], r["plant"]): r for r in client.get("/api/materials").json()}
    assert rows[("1000000003", "1300")]["unit"] is None
    assert rows[("1000000001", "1500")]["material_description"] == "BEARING, BALL 6205"


def test_classification_filter(client):
    oar = client.get("/api/materials", params={"classification": "OAR"}).json()
    non_oar = client.get("/api/materials", params={"classification": "NON_OAR"}).json()
    assert keys(oar) == [("1000000001", "1300"), ("1000000001", "1500")]
    assert keys(non_oar) == [("1000000002", "1300"), ("1000000003", "1300")]


def test_plant_filter(client):
    assert keys(client.get("/api/materials", params={"plant": "1500"}).json()) == [("1000000001", "1500")]


def test_search_by_number_prefix_and_by_words(client):
    by_number = client.get("/api/materials", params={"search": "0001000000002"}).json()
    assert keys(by_number) == [("1000000002", "1300")]
    by_words = client.get("/api/materials", params={"search": "ball bearing"}).json()
    assert keys(by_words) == [("1000000001", "1300"), ("1000000001", "1500")]


def test_paging(client):
    response = client.get("/api/materials", params={"limit": 2, "offset": 2})
    assert keys(response.json()) == [("1000000002", "1300"), ("1000000003", "1300")]
    assert response.headers["X-Total-Count"] == "4"


def test_counts_ignore_classification_but_follow_the_other_filters(client):
    body = client.get("/api/materials/counts").json()
    assert (body["total"], body["oar"], body["non_oar"]) == (4, 2, 2)
    assert body["loaded_at"]
    in_1300 = client.get("/api/materials/counts", params={"plant": "1300"}).json()
    assert (in_1300["total"], in_1300["oar"], in_1300["non_oar"]) == (3, 1, 2)


def test_excel_holds_the_whole_filtered_list(client):
    response = client.get("/api/materials/export.xlsx", params={"classification": "NON_OAR"})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('attachment; filename="materials-stock-')
    sheet = load_workbook(BytesIO(response.content)).active
    values = [list(r) for r in sheet.iter_rows(values_only=True)]
    assert values[0] == ["Material", "Description", "Plant", "Plant name", "Classification", "Stock on hand", "Unit"]
    assert values[1:] == [
        ["1000000002", "GREASE, LITHIUM EP2", "1300", "Black Mountain Mining", "Non-OAR", 3, "KG"],
        ["1000000003", "BOLT, HEX M12", "1300", "Black Mountain Mining", "Non-OAR", 0, None],
    ]
