"""Materials flagged for deletion in SAP (MARC/MARA-LVORM = X) are not OAR.

The real lookup SQL runs against in-memory SQLite views shaped like ``n_marc``
/ ``n_mara``; routing is checked end to end through the same query.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.assistant.router import Flow, route
from app.core.config import get_settings
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.shared.material_scope import MaterialScope, classify_material_scope
from app.shared.material_scope.deletion import deleted_condition, not_deleted_predicate

MARC = [
    # material, plant, mrp_type, mrp_controller, MARC-LVORM
    ("1000000001", "1300", "ND", "OAR", ""),  # active OAR
    ("1000000002", "1300", "ND", "OAR", "X"),  # deleted at this plant
    ("1000000002", "1500", "PD", "OAR", ""),  # ... but not at the other plant
    ("1000000003", "1300", "PD", "OAR", None),  # deleted at client level (MARA)
    ("1000000003", "1500", "ND", "OAR", ""),  # ... so at every plant
    ("1000000004", "1300", "", "OAR", " x "),  # padded / lower case still counts
]
MARA = [
    ("1000000001", ""),
    ("1000000002", None),
    ("1000000003", "X"),
    ("1000000004", ""),
]


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE n_marc (material TEXT, plant TEXT, mrp_type TEXT, mrp_controller TEXT, "
                "df_at_plant_level TEXT)"
            )
        )
        c.execute(text("CREATE TABLE n_mara (material TEXT, df_at_client_level TEXT)"))
        c.execute(text("INSERT INTO n_marc VALUES (:m, :p, :t, :c, :d)"), [dict(zip("mptcd", r)) for r in MARC])
        c.execute(text("INSERT INTO n_mara VALUES (:m, :d)"), [dict(zip("md", r)) for r in MARA])
    with Session(engine) as session:
        yield session


def test_deleted_pairs_are_left_out_and_reported(db):
    deleted: set[tuple[str, str]] = set()
    index = fetch_material_scope_index(db, deleted=deleted)
    assert index == {("1000000001", "1300"): "OAR", ("1000000002", "1500"): "OAR"}
    assert deleted == {
        ("1000000002", "1300"),
        ("1000000003", "1300"),
        ("1000000003", "1500"),
        ("1000000004", "1300"),
    }


def test_only_an_undeleted_oar_controller_is_oar(db):
    """The full I13 rule: DISPO = OAR and not flagged for deletion. A live row
    with another controller is in the index (MARC holds it) but is not OAR."""
    db.execute(text("INSERT INTO n_marc VALUES ('1000000005', '1300', 'PD', 'POD', '')"))
    db.execute(text("INSERT INTO n_marc VALUES ('1000000006', '1300', 'PD', '', '')"))
    index = fetch_material_scope_index(db)
    oar = {key for key, code in index.items() if classify_material_scope(code) is MaterialScope.OAR}
    assert oar == {("1000000001", "1300"), ("1000000002", "1500")}
    assert index[("1000000005", "1300")] == "POD"
    assert index[("1000000006", "1300")] == ""


def test_a_deleted_pair_classifies_excluded(db):
    index = fetch_material_scope_index(db)
    assert classify_material_scope(index.get(("1000000002", "1300"))) is MaterialScope.EXCLUDED
    assert classify_material_scope(index.get(("1000000002", "1500"))) is MaterialScope.OAR


def test_switched_off_keeps_deleted_materials(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "i13_exclude_deleted_materials", False)
    deleted: set[tuple[str, str]] = set()
    index = fetch_material_scope_index(db, deleted=deleted)
    assert len(index) == len(MARC)
    assert deleted == set()
    assert not_deleted_predicate("mc") == ""


def test_the_snapshot_filter_selects_the_same_pairs(db):
    # The work-table predicate and the live lookup must agree.
    rows = db.execute(
        text(f"SELECT material, plant FROM n_marc mc WHERE 1 = 1 {not_deleted_predicate('mc')}")
    ).fetchall()
    assert {(r.material, r.plant) for r in rows} == set(fetch_material_scope_index(db))


def test_a_duplicated_mara_row_does_not_multiply_marc_rows(db):
    db.execute(text("INSERT INTO n_mara VALUES ('1000000001', '')"))
    rows = db.execute(text(f"SELECT material FROM n_marc mc WHERE NOT {deleted_condition('mc')}")).fetchall()
    assert len(rows) == 2


def test_routing_refuses_a_deleted_oar_material_with_the_reason(db):
    decision = route(db, "1000000002", "1300")
    assert decision.flow is Flow.NONE
    assert decision.flagged_for_deletion is True
    assert "flagged for deletion" in decision.reason


def test_routing_still_offers_it_where_it_is_not_deleted(db):
    decision = route(db, "1000000002", "1500")
    assert decision.flow is Flow.I13
    assert decision.flagged_for_deletion is False
