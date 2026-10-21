"""The department codes: where they come from, their names, and the check behind them.

No database: the loader is given a fake session, and the list is set directly.
The rules worth pinning are the ones that keep the form usable -- a plant with
no codes still gets a list, a list that has not loaded still lets a 5-digit code
through, a code listed at the other plant still passes, and a session opened
from SAP is never refused over its department.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from app.assistant import departments
from app.assistant.departments import DepartmentError, check, for_plant, load_all, name_for
from app.core.db import get_db
from app.main import app


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows=(), *, fail=False):
        self.rows = list(rows)
        self.fail = fail
        self.rolled_back = False

    def execute(self, *_args, **_kwargs):
        if self.fail:
            raise ProgrammingError("SELECT", {}, Exception("Invalid object name 'n_dept_codes'"))
        return _Rows(self.rows)

    def rollback(self):
        self.rolled_back = True


@pytest.fixture
def codes(monkeypatch):
    """Set the loaded list by hand; nothing loads in the background."""
    monkeypatch.setattr(departments, "_ensure_loading", lambda: None)

    def set_codes(mapping: dict[str, set[str]], names: dict[str, str] | None = None) -> None:
        monkeypatch.setattr(
            departments, "_codes", {code: frozenset(plants) for code, plants in mapping.items()}
        )
        monkeypatch.setattr(departments, "_names", dict(names or {}))

    set_codes({})
    return set_codes


class TestLoad:
    def test_only_five_digit_codes_at_an_in_scope_plant_are_departments(self) -> None:
        db = _FakeDb(
            [
                ("1300", " 10100 ", "FINANCE-COMMERCIAL DEPARTMENT"),
                (" 1500 ", "40301", "GAMSBERG OPS - MINING"),
                ("1300", "OAR", "BMM INVENTORY CONTROLLER - OAR"),  # a desk, not a department
                ("1300", "1010", "FOUR DIGITS"),
                ("1300", "101000", "SIX DIGITS"),
                (None, "10200", "NO PLANT"),
                ("1300", "", None),
            ]
        )

        loaded, names, complete = load_all(db)

        assert complete
        assert loaded == {"10100": {"1300"}, "40301": {"1500"}}
        assert names == {"10100": "FINANCE-COMMERCIAL DEPARTMENT", "40301": "GAMSBERG OPS - MINING"}

    def test_a_code_listed_at_both_plants_has_both_and_one_name(self) -> None:
        db = _FakeDb(
            [
                ("1300", "10708", "BMM EXPLORATION DEPARTMENT"),
                ("1500", "10708", "BMM EXPLORATION DEPARTMENT"),
            ]
        )

        loaded, names, _ = load_all(db)

        assert loaded == {"10708": {"1300", "1500"}}
        assert names == {"10708": "BMM EXPLORATION DEPARTMENT"}

    def test_a_code_without_a_name_is_listed_unnamed(self) -> None:
        """10107 and 40603 were added by hand, before VZI named them."""
        db = _FakeDb([("1300", "10107", None), ("1500", "40603", "  ")])

        loaded, names, _ = load_all(db)

        assert set(loaded) == {"10107", "40603"}
        assert names == {}

    def test_an_unreadable_view_is_an_empty_incomplete_load(self) -> None:
        db = _FakeDb(fail=True)

        assert load_all(db) == ({}, {}, False)
        assert db.rolled_back


class TestForPlant:
    def test_the_plants_own_codes_come_first(self, codes) -> None:
        codes({"40301": {"1500"}, "10100": {"1300", "1500"}, "10501": {"1300"}})

        listed = for_plant("1300")

        assert [(d.code, d.primary) for d in listed] == [
            ("10100", True),
            ("10501", True),
            ("40301", False),
        ]
        assert listed[0].plants == ("1300", "1500")

    def test_each_code_carries_its_name(self, codes) -> None:
        codes({"10100": {"1300"}, "10107": {"1300"}}, {"10100": "FINANCE-COMMERCIAL DEPARTMENT"})

        named = {d.code: d.name for d in for_plant("1300")}

        assert named == {"10100": "FINANCE-COMMERCIAL DEPARTMENT", "10107": None}

    def test_a_plant_with_no_codes_is_offered_every_code(self, codes) -> None:
        """An empty list would make the form impossible to complete."""
        codes({"10100": {"1300"}, "10501": {"1300"}})

        assert all(d.primary for d in for_plant("1500"))

    def test_no_plant_offers_every_code(self, codes) -> None:
        codes({"10100": {"1300"}, "40301": {"1500"}})

        assert all(d.primary for d in for_plant(None))


class TestNameFor:
    def test_a_named_code(self, codes) -> None:
        codes({"10100": {"1300"}}, {"10100": "FINANCE-COMMERCIAL DEPARTMENT"})
        assert name_for(" 10100 ") == "FINANCE-COMMERCIAL DEPARTMENT"

    @pytest.mark.parametrize("value", [None, "", "10107", "Concentrator"])
    def test_anything_else_has_no_name(self, codes, value) -> None:
        codes({"10100": {"1300"}, "10107": {"1300"}}, {"10100": "FINANCE-COMMERCIAL DEPARTMENT"})
        assert name_for(value) is None


class TestCheck:
    @pytest.mark.parametrize("value", [None, ""])
    def test_not_stated_always_passes(self, codes, value) -> None:
        codes({"10100": {"1300"}})
        check(value, from_sap=False)

    def test_a_listed_code_passes(self, codes) -> None:
        codes({"10100": {"1300"}})
        check("10100", from_sap=False)

    def test_a_code_listed_at_the_other_plant_passes(self, codes) -> None:
        """Only the screen orders by plant: 10701 is listed at 1500 and used at 1300."""
        codes({"10701": {"1500"}})
        check("10701", from_sap=False)

    def test_a_listed_code_without_a_name_passes(self, codes) -> None:
        codes({"10107": {"1300"}})
        check("10107", from_sap=False)

    @pytest.mark.parametrize("value", ["Concentrator", "1010", "10100A", "PLANT METS", "OAR"])
    def test_free_text_is_refused(self, codes, value) -> None:
        codes({"10100": {"1300"}})
        with pytest.raises(DepartmentError, match="not a department code"):
            check(value, from_sap=False)

    def test_an_unlisted_code_is_refused(self, codes) -> None:
        codes({"10100": {"1300"}})
        with pytest.raises(DepartmentError, match="not on the department list"):
            check("99999", from_sap=False)

    def test_before_the_list_loads_any_five_digits_pass(self, codes) -> None:
        """A slow or failed load must not stop the screen working."""
        codes({})
        check("99999", from_sap=False)
        with pytest.raises(DepartmentError):
            check("Concentrator", from_sap=False)

    @pytest.mark.parametrize("value", ["Concentrator", "99999", "10100"])
    def test_sap_is_never_refused(self, codes, value) -> None:
        codes({"10100": {"1300"}})
        check(value, from_sap=True)

    def test_switched_off_nothing_is_refused(self, codes) -> None:
        codes({"10100": {"1300"}})
        check("Concentrator", from_sap=False, enabled=False)


class TestRoutes:
    @pytest.fixture
    def client(self):
        app.dependency_overrides[get_db] = lambda: _FakeDb()
        yield TestClient(app)
        app.dependency_overrides.pop(get_db, None)

    def test_the_list_is_served_camel_cased_with_names(self, codes, client) -> None:
        codes(
            {"10100": {"1300", "1500"}, "40603": {"1500"}},
            {"10100": "FINANCE-COMMERCIAL DEPARTMENT"},
        )

        response = client.get("/api/assistant/departments", params={"plant": "1500"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["plant"] == "1500"
        assert body["loaded"] is True
        assert body["items"] == [
            {
                "code": "10100",
                "name": "FINANCE-COMMERCIAL DEPARTMENT",
                "plants": ["1300", "1500"],
                "primary": True,
            },
            {"code": "40603", "name": None, "plants": ["1500"], "primary": True},
        ]

    def test_not_loaded_says_so(self, codes, client) -> None:
        codes({})

        body = client.get("/api/assistant/departments").json()

        assert body == {"plant": None, "items": [], "loaded": False}

    def test_the_platform_opener_cannot_start_a_session_with_free_text(
        self, codes, client
    ) -> None:
        """Refused before routing: nothing is written to an append-only table.
        Free text belongs in ``departmentNote``, not in ``department``."""
        codes({"10100": {"1300"}})

        response = client.post(
            "/api/assistant/sessions",
            headers={"X-Actor-Id": "departments-test"},
            json={"materialId": "8000005632", "plant": "1300", "department": "Concentrator"},
        )

        assert response.status_code == 422, response.text
        assert "not a department code" in response.json()["detail"]

    def test_a_department_note_over_200_characters_is_refused(self, codes, client) -> None:
        codes({"10100": {"1300"}})

        response = client.post(
            "/api/assistant/sessions",
            headers={"X-Actor-Id": "departments-test"},
            json={
                "materialId": "8000005632",
                "plant": "1300",
                "department": "10100",
                "departmentNote": "x" * 201,
            },
        )

        assert response.status_code == 422, response.text
