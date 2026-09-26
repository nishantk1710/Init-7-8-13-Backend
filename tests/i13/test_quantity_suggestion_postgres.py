"""W7.4 integration tests: the quantity-suggestion store, its API, and the
W6.6 bridge, against real seeded Postgres data.

Skipped outright when no ``DATABASE_URL`` is configured -- the same
convention as the other Postgres-gated I13 test files. The arithmetic itself
is covered by ``test_quantity_suggestion.py``, which needs no database; what
this file adds is what only real data and the real persistence path can
prove: that the engine reads W6.3's mart rather than recomputing anything,
that a suggestion survives a round trip with its config snapshot intact, and
that W6.6's quantity-override rule -- built and left idle -- now fires.

Every row written here is deleted again at the end of the test, so running
the file repeatedly leaves the shared database as it found it.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i13.act.domain import ExceptionStatus, ExceptionType
from app.initiatives.i13.act.service import detect_exceptions
from app.initiatives.i13.config import QuantitySuggestionConfig
from app.initiatives.i13.quantity_suggestion import SuggestionDirection, SuggestionReason
from app.initiatives.i13.quantity_suggestion_store import (
    WatchMetricNotFoundError,
    add_justification,
    build_quantity_decision_records,
    issue_quantity_suggestion,
    list_justifications,
    list_quantity_suggestions,
    record_acceptance,
)
from app.main import app
from app.models.i13_quantity_suggestion import QuantityJustificationRecord, QuantitySuggestionRecord
from app.models.i13_watch_mart import WatchMetricMart
from tests.i13.conftest import FakeExceptionRepository, FakeNotificationPort

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = [needs_db, pytest.mark.needs_seed_data]

AS_OF = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

client = TestClient(app)


def _config(*, ceiling: str = "6", minimum_history: int = 4) -> QuantitySuggestionConfig:
    return QuantitySuggestionConfig(
        enabled=True,
        cover_ceiling_months=Decimal(ceiling),
        minimum_history_count=minimum_history,
        lookback_months=12,
    )


@pytest.fixture
def session():
    """A session whose W7.4 writes are rolled back out of the shared database
    when the test ends, whether it passed or failed."""
    created: list[str] = []
    with get_sessionmaker()() as db:
        db.info["created_suggestions"] = created
        try:
            yield db
        finally:
            db.rollback()
            if created:
                db.execute(
                    delete(QuantityJustificationRecord).where(
                        QuantityJustificationRecord.suggestion_id.in_(created)
                    )
                )
                db.execute(
                    delete(QuantitySuggestionRecord).where(QuantitySuggestionRecord.suggestion_id.in_(created))
                )
                db.commit()


def _issue(db, config=None, **kwargs) -> QuantitySuggestionRecord:
    record = issue_quantity_suggestion(db, config or _config(), as_of=AS_OF, **kwargs)
    db.info["created_suggestions"].append(record.suggestion_id)
    db.commit()
    return record


def _mart_row_with_history(db) -> WatchMetricMart:
    """A real material-plant the engine will actually speak about. Chosen by
    query, not hard-coded: which materials clear the history bar is a
    property of the seeded data, not of this test."""
    row = db.execute(
        select(WatchMetricMart)
        .where(WatchMetricMart.average_monthly_consumption > 0, WatchMetricMart.consumption_count_12m >= 4)
        .order_by(WatchMetricMart.material, WatchMetricMart.plant)
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        pytest.skip("no WATCH mart row with consumption history -- run the W6.3 refresh first")
    return row


def _mart_row_without_history(db) -> WatchMetricMart:
    row = db.execute(
        select(WatchMetricMart)
        .where(WatchMetricMart.average_monthly_consumption == 0)
        .order_by(WatchMetricMart.material, WatchMetricMart.plant)
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        pytest.skip("no WATCH mart row without consumption history in the seeded data")
    return row


# --- Reading W6.3, not recomputing ---------------------------------------


def test_the_suggestion_snapshots_the_w63_mart_figures_unchanged(session) -> None:
    """W7.4 never re-derives a consumption rate, re-reads MARD or re-nets an
    open PO -- it reuses W6.3's figures exactly, the way ACT reuses its GRNI
    flag."""
    mart = _mart_row_with_history(session)

    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("1"),
        plan_window_months=Decimal("3"),
    )

    assert record.average_monthly_consumption == mart.average_monthly_consumption
    assert record.stock_on_hand == (mart.stock_on_hand or Decimal("0"))
    assert record.open_po_quantity == mart.open_po_quantity
    assert record.consumption_count_12m == mart.consumption_count_12m


def test_missing_mart_row_raises_rather_than_assuming_an_empty_store(session) -> None:
    """An absent mart row means the WATCH refresh has not run for this
    material -- not that it has no stock and no consumption. Guessing the
    latter would put a confident purchase figure on data never computed."""
    with pytest.raises(WatchMetricNotFoundError):
        issue_quantity_suggestion(
            session,
            _config(),
            material="NO-SUCH-MATERIAL",
            plant="0000",
            requested_quantity=Decimal("1"),
            plan_window_months=Decimal("3"),
            as_of=AS_OF,
        )


# --- The two nudges, on real data ----------------------------------------


def test_ceiling_nudge_on_real_data(session) -> None:
    mart = _mart_row_with_history(session)
    # Far beyond any plausible cover for this material, so the ceiling is
    # certain to bite whatever the seeded figures happen to be.
    absurd = (mart.average_monthly_consumption * 1000) + Decimal("1000")

    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=absurd,
        plan_window_months=Decimal("1"),
    )

    assert record.direction == SuggestionDirection.DOWN.value
    assert record.reason_code in (
        SuggestionReason.EXCEEDS_COVER_CEILING.value,
        SuggestionReason.CEILING_BELOW_PLAN_NEED.value,
    )
    assert record.suggested_quantity == record.ceiling_quantity
    assert record.suggested_quantity < record.requested_quantity


def test_shortfall_nudge_on_real_data(session) -> None:
    """A plan window two months longer than what is already covered, under a
    ceiling one month wider still -- so the shortfall, not the ceiling, is
    what the engine reacts to."""
    mart = _mart_row_with_history(session)
    amc = mart.average_monthly_consumption
    covered_months = ((mart.stock_on_hand or Decimal("0")) + mart.open_po_quantity) / amc

    record = _issue(
        session,
        _config(ceiling=str(covered_months + 3)),
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("0"),
        plan_window_months=covered_months + 2,
    )

    assert record.direction == SuggestionDirection.UP.value
    assert record.reason_code == SuggestionReason.BELOW_PLAN_NEED.value
    assert record.suggested_quantity == record.net_need_quantity
    assert record.suggested_quantity > 0


def test_declines_where_history_is_below_the_minimum_on_real_data(session) -> None:
    mart = _mart_row_without_history(session)

    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("50"),
        plan_window_months=Decimal("3"),
    )

    assert record.direction == SuggestionDirection.NO_SUGGESTION.value
    assert record.reason_code == SuggestionReason.INSUFFICIENT_HISTORY.value
    assert record.suggested_quantity is None


def test_unset_config_disables_the_engine_rather_than_defaulting(session) -> None:
    mart = _mart_row_with_history(session)

    record = _issue(
        session,
        QuantitySuggestionConfig(
            enabled=True, cover_ceiling_months=None, minimum_history_count=None, lookback_months=12
        ),
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("99999"),
        plan_window_months=Decimal("3"),
    )

    assert record.reason_code == SuggestionReason.NOT_CONFIGURED.value
    assert record.suggested_quantity is None
    assert record.cover_ceiling_months is None


# --- Round trip, outcome, provenance -------------------------------------


def test_the_config_snapshot_survives_the_round_trip(session) -> None:
    """Retuning the ceiling later must not rewrite the basis of a suggestion
    already made -- FRS §8 has to prove what was suggested and on what
    basis."""
    mart = _mart_row_with_history(session)
    record = _issue(
        session,
        _config(ceiling="7", minimum_history=2),
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("5"),
        plan_window_months=Decimal("2"),
    )

    session.expire_all()
    reloaded = session.get(QuantitySuggestionRecord, record.suggestion_id)

    assert reloaded.cover_ceiling_months == Decimal("7.000000")
    assert reloaded.minimum_history_count == 2
    assert reloaded.lookback_months == 12
    assert reloaded.calculated_at.astimezone(timezone.utc) == AS_OF


def test_the_reason_is_the_deterministic_sentence_when_no_real_provider_is_configured(session) -> None:
    """The default provider is the deterministic stub, whose output is openly
    synthetic placeholder text -- persisting that as the reason a requester
    reads would be worse than the plain sentence."""
    mart = _mart_row_with_history(session)
    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("1"),
        plan_window_months=Decimal("1"),
    )

    if (get_settings().llm_provider or "stub").strip().lower() != "stub":
        pytest.skip("a real LLM provider is configured; the model phrases the reason instead")

    assert record.reason_source == "DETERMINISTIC"
    assert record.reason_model is None
    assert "stub completion" not in record.reason_text


def test_acceptance_records_who_and_when(session) -> None:
    mart = _mart_row_with_history(session)
    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("1"),
        plan_window_months=Decimal("1"),
    )
    assert record.accepted is None, "a fresh suggestion is undecided, not rejected"

    updated = record_acceptance(session, record.suggestion_id, accepted=True, actor_id="TEST-REQ", as_of=AS_OF)
    session.commit()

    assert updated.accepted is True
    assert updated.accepted_by == "TEST-REQ"
    assert updated.accepted_at is not None


def test_accepting_a_declined_suggestion_is_refused(session) -> None:
    """There is nothing to accept when the engine declined, and storing
    accepted=True against it would put a benefit claim behind a figure that
    does not exist."""
    mart = _mart_row_without_history(session)
    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("5"),
        plan_window_months=Decimal("1"),
    )

    with pytest.raises(ValueError, match="nothing to accept"):
        record_acceptance(session, record.suggestion_id, accepted=True, actor_id="TEST-REQ", as_of=AS_OF)


def test_justifications_are_append_only(session) -> None:
    mart = _mart_row_with_history(session)
    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("1"),
        plan_window_months=Decimal("1"),
    )

    add_justification(
        session,
        record.suggestion_id,
        reason_category="SHUTDOWN",
        free_text="Planned shutdown in November needs the full quantity.",
        actor_id="TEST-REQ",
        as_of=AS_OF,
    )
    add_justification(
        session,
        record.suggestion_id,
        reason_category="SHUTDOWN",
        free_text="Revised: shutdown moved to December.",
        actor_id="TEST-REQ",
        as_of=AS_OF,
    )
    session.commit()

    justifications = list_justifications(session, record.suggestion_id)
    assert len(justifications) == 2, "a revised reason adds a row; the earlier one stays readable"
    assert justifications[-1].free_text.startswith("Revised")


def test_listing_is_paginated(session) -> None:
    mart = _mart_row_with_history(session)
    for _ in range(3):
        _issue(
            session,
            material=mart.material,
            plant=mart.plant,
            requested_quantity=Decimal("1"),
            plan_window_months=Decimal("1"),
        )

    page = list_quantity_suggestions(session, material=mart.material, plant=mart.plant, limit=2, offset=0)
    assert len(page) == 2

    next_page = list_quantity_suggestions(session, material=mart.material, plant=mart.plant, limit=2, offset=2)
    assert {r.suggestion_id for r in page}.isdisjoint({r.suggestion_id for r in next_page})


# --- The W6.6 bridge: the idle path finally fires -------------------------


def _detect(records) -> tuple[FakeExceptionRepository, object]:
    """Run W6.6 detection over quantity decisions alone, with an in-memory
    exception repository -- this proves the rule fires without writing
    exception rows into the shared database."""
    repository = FakeExceptionRepository()
    result = detect_exceptions(
        AS_OF,
        ledger_entries=[],
        plans=[],
        grni_snapshots={},
        repository=repository,
        notification_port=FakeNotificationPort(),
        plan_breach_grace_days=14,
        requester_response_days=5,
        quantity_decision_records=records,
    )
    return repository, result


def test_a_rejected_suggestion_raises_a_w66_quantity_override_exception(session) -> None:
    """The package's point of arrival: W6.6 built QUANTITY_OVERRIDE detection
    and left it idle because ``suggested_quantity`` was None for every caller
    ("the suggestion engine is W7.4"). This is that path firing."""
    mart = _mart_row_with_history(session)
    absurd = (mart.average_monthly_consumption * 1000) + Decimal("1000")
    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=absurd,
        plan_window_months=Decimal("1"),
        reservation_number="TEST-RES-W74",
        reservation_item="0010",
        requester_id="TEST-REQ",
    )
    record_acceptance(session, record.suggestion_id, accepted=False, actor_id="TEST-REQ", as_of=AS_OF)
    add_justification(
        session,
        record.suggestion_id,
        reason_category="SHUTDOWN",
        free_text="Bulk buy agreed with the vendor.",
        actor_id="TEST-REQ",
        as_of=AS_OF,
    )
    session.commit()

    decisions = build_quantity_decision_records(session, material=mart.material, plant=mart.plant)
    mine = [d for d in decisions if d.reservation_number == "TEST-RES-W74"]
    assert len(mine) == 1

    decision = mine[0]
    assert decision.suggested_quantity == record.suggested_quantity
    assert decision.requested_quantity == record.requested_quantity, "they kept their own figure"
    assert decision.override_justification == "SHUTDOWN: Bulk buy agreed with the vendor."

    repository, result = _detect(mine)
    assert result.created == 1
    assert result.routed == 1, "the requester_id W7.4 carries is what lets W6.6 route it to them"

    raised = repository.list(exception_type=ExceptionType.QUANTITY_OVERRIDE)
    assert len(raised) == 1
    exception = raised[0]
    # Routed straight to the requester rather than left unassigned, because
    # the suggestion recorded who asked.
    assert exception.status is ExceptionStatus.AWAITING_REQUESTER
    assert exception.owner_requester_id == "TEST-REQ"
    assert exception.material == mart.material
    assert exception.evidence["suggested_quantity"] == str(record.suggested_quantity)
    assert exception.evidence["suggested_quantity"] != "SOURCE_UNAVAILABLE", "W7.4 is the source; it answered"
    assert exception.evidence["override_justification"] == "SHUTDOWN: Bulk buy agreed with the vendor."


def test_an_accepted_suggestion_raises_no_exception(session) -> None:
    """Accepting means taking the suggested figure, so there is no deviation
    to flag -- the requester did what the engine advised."""
    mart = _mart_row_with_history(session)
    absurd = (mart.average_monthly_consumption * 1000) + Decimal("1000")
    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=absurd,
        plan_window_months=Decimal("1"),
        reservation_number="TEST-RES-W74-OK",
        reservation_item="0010",
    )
    record_acceptance(session, record.suggestion_id, accepted=True, actor_id="TEST-REQ", as_of=AS_OF)
    session.commit()

    decisions = [
        d
        for d in build_quantity_decision_records(session, material=mart.material, plant=mart.plant)
        if d.reservation_number == "TEST-RES-W74-OK"
    ]
    assert decisions[0].requested_quantity == record.suggested_quantity, "they took the suggestion"

    repository, result = _detect(decisions)
    assert result.created == 0
    assert repository.list(exception_type=ExceptionType.QUANTITY_OVERRIDE) == []


def test_an_undecided_suggestion_is_not_yet_a_decision(session) -> None:
    """Flagging an override against a suggestion nobody has answered would
    mean flagging a requester for a decision they have not made."""
    mart = _mart_row_with_history(session)
    absurd = (mart.average_monthly_consumption * 1000) + Decimal("1000")
    record = _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=absurd,
        plan_window_months=Decimal("1"),
        reservation_number="TEST-RES-W74-PENDING",
        reservation_item="0010",
    )
    assert record.accepted is None

    decisions = build_quantity_decision_records(session, material=mart.material, plant=mart.plant)
    assert not [d for d in decisions if d.reservation_number == "TEST-RES-W74-PENDING"]


def test_a_declined_suggestion_never_reaches_w66(session) -> None:
    """NO_SUGGESTION carries no figure to compare against, and acceptance
    cannot be recorded on it, so it can never become an override."""
    mart = _mart_row_without_history(session)
    _issue(
        session,
        material=mart.material,
        plant=mart.plant,
        requested_quantity=Decimal("5"),
        plan_window_months=Decimal("1"),
        reservation_number="TEST-RES-W74-NOSUG",
        reservation_item="0010",
    )

    decisions = build_quantity_decision_records(session, material=mart.material, plant=mart.plant)
    assert not [d for d in decisions if d.reservation_number == "TEST-RES-W74-NOSUG"]


# --- The API surface ------------------------------------------------------


def test_api_round_trip(session) -> None:
    """Issue, read back, justify and accept through the real request path.

    Runs under whatever config the environment actually has: with the VZI
    ceiling and minimum history still unset (FRS §10), the engine declines
    with NOT_CONFIGURED and acceptance is refused -- which is the behaviour
    this package ships with until those two numbers arrive.
    """
    mart = _mart_row_with_history(session)

    created = client.post(
        "/api/i13/quantity-suggestion",
        json={
            "material": mart.material,
            "plant": mart.plant,
            "requested_quantity": "42",
            "plan_window_months": "3",
            "reservation_number": "TEST-RES-W74-API",
            "reservation_item": "0010",
        },
        headers={"X-Actor-Id": "TEST-REQ"},
    )
    assert created.status_code == 201
    body = created.json()
    suggestion_id = body["suggestion_id"]
    session.info["created_suggestions"].append(suggestion_id)

    assert body["requested_quantity"] == "42.000000"
    assert body["accepted"] is None
    assert body["lookback_months"] == get_settings().i13_qty_lookback_months

    fetched = client.get(f"/api/i13/quantity-suggestion/{suggestion_id}")
    assert fetched.status_code == 200
    assert fetched.json()["justifications"] == []

    justified = client.post(
        f"/api/i13/quantity-suggestion/{suggestion_id}/justification",
        json={"reason_category": "SHUTDOWN", "free_text": "Planned November shutdown."},
        headers={"X-Actor-Id": "TEST-REQ"},
    )
    assert justified.status_code == 200
    assert justified.json()["justifications"][0]["actor_id"] == "TEST-REQ"

    accepted = client.post(
        f"/api/i13/quantity-suggestion/{suggestion_id}/acceptance",
        json={"accepted": True},
        headers={"X-Actor-Id": "TEST-REQ"},
    )
    if body["suggested_quantity"] is None:
        assert accepted.status_code == 409, "nothing to accept where the engine declined"
    else:
        assert accepted.status_code == 200
        assert accepted.json()["accepted"] is True


def test_api_listing_is_paginated() -> None:
    response = client.get("/api/i13/quantity-suggestion", params={"limit": 5})
    assert response.status_code == 200
    assert len(response.json()) <= 5


def test_api_listing_refuses_an_unbounded_limit() -> None:
    assert client.get("/api/i13/quantity-suggestion", params={"limit": 100000}).status_code == 422


def test_api_unknown_suggestion_is_404() -> None:
    assert client.get("/api/i13/quantity-suggestion/does-not-exist").status_code == 404
    assert (
        client.post(
            "/api/i13/quantity-suggestion/does-not-exist/acceptance",
            json={"accepted": True},
            headers={"X-Actor-Id": "TEST-REQ"},
        ).status_code
        == 404
    )


def test_api_unknown_material_plant_is_404() -> None:
    response = client.post(
        "/api/i13/quantity-suggestion",
        json={
            "material": "NO-SUCH-MATERIAL",
            "plant": "0000",
            "requested_quantity": "1",
            "plan_window_months": "1",
        },
    )
    assert response.status_code == 404
    assert "W6.3" in response.json()["detail"]


def test_api_negative_quantity_is_refused() -> None:
    response = client.post(
        "/api/i13/quantity-suggestion",
        json={"material": "X", "plant": "1300", "requested_quantity": "-1", "plan_window_months": "1"},
    )
    assert response.status_code == 422
