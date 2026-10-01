"""I13_REFERENCE_PLANS_ENABLED: the generator's consumption_plans.csv is read
only when someone switches it on.

The file ships with every deploy (the package is the whole repository), so a
reader that keyed off "the file exists" counted 742 fabricated plans in
production -- every open one a permanent plan breach. These pin the switch,
not the file, as what decides.
"""

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.initiatives.i13.plans import load_consumption_plans, load_reference_plans
from tests.i13.conftest import write_csv

PLAN_HEADER = [
    "plan_id", "session_id", "Rsnum", "Rspos", "Matnr", "Werks", "requester", "purpose",
    "planned_quantity", "planned_use_date", "status",
]
PLAN_ROW = {
    "plan_id": "PLAN-1", "session_id": "SESS-1", "Rsnum": "1000000000", "Rspos": "0001",
    "Matnr": "MAT1", "Werks": "1000", "requester": "REQ1", "purpose": "test",
    "planned_quantity": "10", "planned_use_date": "2026-01-01", "status": "OPEN",
}


@pytest.fixture
def plans_dir(tmp_path: Path) -> Path:
    write_csv(tmp_path / "platform" / "consumption_plans.csv", PLAN_HEADER, [PLAN_ROW])
    return tmp_path


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch):
    def set_to(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("I13_REFERENCE_PLANS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("I13_REFERENCE_PLANS_ENABLED", value)
        get_settings.cache_clear()

    yield set_to
    monkeypatch.undo()
    get_settings.cache_clear()


def test_off_by_default_even_with_the_file_present(plans_dir: Path, switch) -> None:
    switch(None)

    assert get_settings().i13_reference_plans_enabled is False
    assert load_reference_plans(plans_dir) == []
    assert load_consumption_plans(plans_dir) == []


def test_switched_on_the_file_is_read_and_labelled_fabricated(plans_dir: Path, switch) -> None:
    switch("true")

    plans = load_reference_plans(plans_dir)

    assert [p.plan_id for p in plans] == ["PLAN-1"]
    assert all(p.is_fabricated for p in plans)


def test_switched_on_a_missing_file_is_still_no_plans(tmp_path: Path, switch) -> None:
    switch("true")

    assert load_reference_plans(tmp_path) == []
