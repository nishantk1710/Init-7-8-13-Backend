"""A rerun must not put a fresh recommendation on top of one that is in the approval chain.

The pages and the notifications read the newest recommendation row per material-plant. A pipeline rerun with new
upstream run ids writes a new row per material-plant, starting at READY_FOR_REVIEW, so without this rule a material
that approvers are deciding on, or that is approved and waiting for SAP, would be hidden behind a new proposal.
"""

from types import SimpleNamespace

from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import repository, service
from app.initiatives.i7.recommendations.rationale import RationaleResult
from app.initiatives.i7.recommendations.types import PROTECTED_STATUSES, LifecycleStatus


def test_the_protected_statuses_are_the_chain_and_the_approved_ones():
    assert PROTECTED_STATUSES == {
        LifecycleStatus.PENDING_APPROVAL,
        LifecycleStatus.HELD,
        LifecycleStatus.ADJUSTED,
        LifecycleStatus.SENT_BACK,
        LifecycleStatus.APPROVED,
        LifecycleStatus.SAP_EXECUTION_PENDING,
    }


def test_a_material_is_released_once_it_is_executed_and_adopted_in_sap():
    for released in (
        LifecycleStatus.SAP_EXECUTED,
        LifecycleStatus.ADOPTED,
        LifecycleStatus.PARTIALLY_ADOPTED,
        LifecycleStatus.NOT_ADOPTED,
    ):
        assert released not in PROTECTED_STATUSES


def test_proposals_that_have_not_started_or_were_declined_still_refresh():
    for refreshes in (
        LifecycleStatus.NOT_EVALUABLE,
        LifecycleStatus.READY_FOR_REVIEW,
        LifecycleStatus.REJECTED,
    ):
        assert refreshes not in PROTECTED_STATUSES


class FakeSession:
    added: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, row):
        FakeSession.added.append(row)

    def commit(self):
        pass


def test_a_protected_material_plant_is_not_built_explained_or_written(monkeypatch):
    FakeSession.added = []
    rows = [
        SimpleNamespace(sap_material_number="IN_APPROVAL", sap_plant_code="1300"),
        SimpleNamespace(sap_material_number="FREE", sap_plant_code="1300"),
        # The same material at another plant is a different material-plant and is not held.
        SimpleNamespace(sap_material_number="IN_APPROVAL", sap_plant_code="1500"),
    ]
    built_for: list[tuple[str, str]] = []
    explained: list[str] = []

    def build(row, policy):
        built_for.append((row.sap_material_number, row.sap_plant_code))
        return SimpleNamespace(
            sap_material_number=row.sap_material_number, sap_plant_code=row.sap_plant_code
        )

    def explain(built, workers):
        explained.extend(item.sap_material_number for item in built)
        return [RationaleResult(text="why", source="DETERMINISTIC_FALLBACK") for _ in built]

    def to_row(item, outcome):
        return {
            "sap_material_number": item.sap_material_number,
            "sap_plant_code": item.sap_plant_code,
            "feature_run_id": 1,
            "forecast_run_id": 1,
            "inventory_run_id": 1,
            "oar_run_id": None,
            "policy_id": "p",
            "policy_version": 1,
            "formula_version": 1,
            "status": "READY_FOR_REVIEW",
        }

    seen_statuses: list = []

    def protected(session, statuses):
        seen_statuses.extend(statuses)
        return {("IN_APPROVAL", "1300")}

    monkeypatch.setattr(service, "get_sessionmaker", lambda: FakeSession)
    monkeypatch.setattr(service.repository, "latest_inventory_run", lambda s: 3)
    monkeypatch.setattr(service.repository, "latest_oar_run", lambda s: None)
    monkeypatch.setattr(service.repository, "forecast_run_for_inventory_run", lambda s, run: 2)
    monkeypatch.setattr(service.repository, "load_normal_inputs", lambda s, inv, fc: rows)
    monkeypatch.setattr(service.repository, "load_protected_material_plants", protected)
    monkeypatch.setattr(service.builder, "build_normal_recommendation", build)
    monkeypatch.setattr(service, "_generate_rationales", explain)
    monkeypatch.setattr(service, "_to_row", to_row)

    result = service.generate_recommendations(PolicyDocument())

    assert result.error is None
    assert built_for == [("FREE", "1300"), ("IN_APPROVAL", "1500")]
    assert explained == ["FREE", "IN_APPROVAL"]  # the held one costs no AI call
    assert [(r.sap_material_number, r.sap_plant_code) for r in FakeSession.added] == [
        ("FREE", "1300"),
        ("IN_APPROVAL", "1500"),
    ]
    assert result.recommendations_written == 2
    assert result.held_in_approval == 1
    assert set(seen_statuses) == PROTECTED_STATUSES


def test_the_repository_asks_for_the_newest_row_in_the_given_statuses():
    captured = {}

    class Session:
        def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return SimpleNamespace(all=lambda: [("M1", "1300"), ("M2", "1500")])

    found = repository.load_protected_material_plants(
        Session(), [LifecycleStatus.APPROVED, LifecycleStatus.HELD]
    )

    assert found == {("M1", "1300"), ("M2", "1500")}
    assert captured["params"] == {"statuses": ["APPROVED", "HELD"]}
    assert "MAX(id)" in captured["sql"]  # the newest row per material-plant, as the pages read it


def test_no_statuses_means_no_query():
    class Session:
        def execute(self, *args, **kwargs):
            raise AssertionError("queried with nothing to look for")

    assert repository.load_protected_material_plants(Session(), []) == set()
