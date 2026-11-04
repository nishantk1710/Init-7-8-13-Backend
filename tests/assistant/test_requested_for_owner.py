"""Who owns an assistant quantity override: the person named in the Requester box, when known.

``requested_for`` is free text and never identity, so it only decides the owner (and so who gets
"Confirm Exception") when ``I13_REQUESTER_RECIPIENTS`` says who that name stands for. Otherwise the
exception stays with the person who operated the assistant. Built on the real request from
``test_requests`` (OAR_ABOVE: the requester keeps 10 against a suggested 8, an override), on SQLite.
"""

from __future__ import annotations

import pytest

from app.assistant import requests as request_service
from app.initiatives.i13.quantity_suggestion_store import (
    build_assistant_quantity_decision_records,
    owner_for_assistant_decision,
)
from tests.assistant.test_requests import (  # noqa: F401 -- pytest fixtures
    OAR_ABOVE,
    PLANT,
    TODAY,
    every_case,
    factory,
    settings,
)

OPERATOR = "00000000-0000-0000-0000-000000000001"
ALIASES = {"darren": "00000000-0000-0000-0000-000000000011"}


class TestOwnerForAssistantDecision:
    def test_the_named_person_owns_it_when_the_name_is_a_known_alias(self) -> None:
        assert owner_for_assistant_decision(OPERATOR, "darren", ALIASES) == "darren"

    def test_matched_ignoring_case_and_spaces_and_kept_as_typed_trimmed(self) -> None:
        assert owner_for_assistant_decision(OPERATOR, "  Darren ", ALIASES) == "Darren"

    @pytest.mark.parametrize("named", [None, "", "   ", "J Miller", "someone else"])
    def test_anyone_else_leaves_it_with_the_operator(self, named) -> None:
        assert owner_for_assistant_decision(OPERATOR, named, ALIASES) == OPERATOR

    @pytest.mark.parametrize("aliases", [None, {}])
    def test_with_no_aliases_nothing_changes(self, aliases) -> None:
        assert owner_for_assistant_decision(OPERATOR, "darren", aliases) == OPERATOR

    def test_no_operator_and_no_alias_is_still_none(self) -> None:
        assert owner_for_assistant_decision(None, "J Miller", ALIASES) is None


def make_request(db, settings, *, requested_for: str | None) -> str:
    result = request_service.create(
        db,
        plant=PLANT,
        lines=every_case(),
        idempotency_key=f"draft-{requested_for}",
        requester=OPERATOR,
        requested_for=requested_for,
        department="10100",
        today=TODAY,
        settings=settings,
    )
    db.commit()
    return result.request.id


def override_record(db, **kwargs):
    records = build_assistant_quantity_decision_records(db, **kwargs)
    return next(r for r in records if r.material == OAR_ABOVE)


def test_the_decision_record_is_owned_by_the_named_person(factory, settings) -> None:
    with factory() as db:
        make_request(db, settings, requested_for="darren")
        assert override_record(db, requester_aliases=ALIASES).requester_id == "darren"


def test_without_the_alias_it_stays_with_the_operator(factory, settings) -> None:
    with factory() as db:
        make_request(db, settings, requested_for="darren")
        assert override_record(db).requester_id == OPERATOR
        assert override_record(db, requester_aliases={"someone": "x"}).requester_id == OPERATOR


def test_with_nobody_named_it_stays_with_the_operator(factory, settings) -> None:
    with factory() as db:
        make_request(db, settings, requested_for=None)
        assert override_record(db, requester_aliases=ALIASES).requester_id == OPERATOR
