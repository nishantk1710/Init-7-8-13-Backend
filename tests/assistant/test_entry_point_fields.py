"""What the entry point asks for, and what it records.

The one thing these tests exist to protect
-------------------------------------------
Two people are involved in a session and they are not the same person. One
coordinator operates the assistant for the whole site; the person who wants the
part is a name that coordinator types in.

So ``requester`` (the operator, taken from the caller) and ``requested_for``
(the typed name, taken from the body) must never be crossed over. If they ever
are, a typed name silently becomes an audit author -- on an append-only table,
where it cannot be corrected. :class:`TestTheTwoPeopleStaySeparate` is the test
that would catch it, and it is the reason this file exists.

The rest covers the other half of the same change: quantity is no longer asked
for at the entry point, and a session minted today must record NULL for it
rather than a zero that would read as "they asked for none".

Skipped without ``DATABASE_URL``, the same convention as the rest of
``tests/assistant``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = needs_db

client = TestClient(app)

#: The operator. Deliberately not a name -- it stands for whoever is signed in,
#: which today is nobody, and using a person's name here would blur the very
#: distinction these tests protect.
OPERATOR = {"X-Actor-Id": "ws7-entry-point-test"}

#: The person the part is for. A name, because that is what gets typed.
REQUESTED_FOR = "T. Mokoena"


@pytest.fixture(scope="module")
def in_scope_material() -> tuple[str, str]:
    """Any material the router will open a session for.

    Read from the extract rather than hard-coded, the same reasoning
    ``test_act_end_to_end`` gives: a fixed material number makes a test a
    statement about one extract instead of about the behaviour it is checking.

    An 80-series part is preferred because the I08 flow needs only the cached
    snapshot, where I13 needs a WATCH row and refuses without one. Either flow
    proves what is being tested here -- these fields are recorded before the
    flow branches.
    """
    with get_sessionmaker()() as db:
        row = db.execute(
            text(
                """
                SELECT material, plant
                FROM raw_marc
                WHERE material LIKE '80%'
                  AND plant IN ('1300', '1500')
                ORDER BY material
                LIMIT 1
                """
            )
        ).fetchone()

    if row is None:
        pytest.skip("no 80-series material in this extract")
    return row[0], row[1]


def _open(material: str, plant: str, **body) -> dict:
    """Open a session and return the response body, skipping if none was minted."""
    response = client.post(
        "/api/assistant/sessions",
        headers=OPERATOR,
        json={"materialId": material, "plant": plant, **body},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    if payload.get("sessionId") is None:
        pytest.skip(f"no session minted: {payload['routing']['reason']}")
    return payload


def _row(session_id: str):
    """The stored session, read back straight from the table.

    Read from the database rather than from the trace endpoint on purpose: the
    point is what was *written*, and a response model that happened to map the
    same value into two fields would pass a test that only read the API.
    """
    with get_sessionmaker()() as db:
        return db.execute(
            text(
                """
                SELECT department, requested_for, requester, requested_quantity
                FROM assistant_session
                WHERE id = :id
                """
            ),
            {"id": session_id},
        ).fetchone()


class TestTheTwoPeopleStaySeparate:
    """The distinction the whole schema change exists for."""

    def test_the_typed_name_and_the_operator_land_in_different_columns(
        self, in_scope_material
    ) -> None:
        material, plant = in_scope_material
        body = _open(material, plant, requestedFor=REQUESTED_FOR)

        row = _row(body["sessionId"])
        assert row.requested_for == REQUESTED_FOR
        assert row.requester == OPERATOR["X-Actor-Id"]

    def test_a_typed_name_never_becomes_the_author(self, in_scope_material) -> None:
        """The failure mode worth naming: `requestedFor` overwriting `requester`.

        That would make a self-declared name the author of an append-only audit
        record, which is the one thing identity is taken from the caller to
        prevent.
        """
        material, plant = in_scope_material
        body = _open(material, plant, requestedFor="SOMEBODY ELSE ENTIRELY")

        row = _row(body["sessionId"])
        assert row.requester == OPERATOR["X-Actor-Id"]
        assert row.requester != "SOMEBODY ELSE ENTIRELY"

    def test_the_trace_serves_both(self, in_scope_material) -> None:
        material, plant = in_scope_material
        body = _open(material, plant, requestedFor=REQUESTED_FOR, department="Concentrator")

        trace = client.get(f"/api/assistant/sessions/{body['sessionId']}", headers=OPERATOR)
        assert trace.status_code == 200, trace.text
        served = trace.json()

        assert served["requestedFor"] == REQUESTED_FOR
        assert served["department"] == "Concentrator"
        # Served but never drawn -- the trace is the FR-8 evidence view, and an
        # audit record without its author is not one.
        assert served["requester"] == OPERATOR["X-Actor-Id"]


class TestTheNewFieldsAreRecorded:
    def test_department_is_stored(self, in_scope_material) -> None:
        material, plant = in_scope_material
        body = _open(material, plant, department="Concentrator")

        assert _row(body["sessionId"]).department == "Concentrator"

    def test_both_are_optional(self, in_scope_material) -> None:
        """A session opened from SAP carries neither, and that must still work.

        The BAdI pop-up has a material and a plant and nothing else. Requiring
        either field would break the SAP entry point before it is built.
        """
        material, plant = in_scope_material
        row = _row(_open(material, plant)["sessionId"])

        assert row.department is None
        assert row.requested_for is None

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_whitespace_is_not_an_answer(self, in_scope_material, blank) -> None:
        """Blank input is stored as NULL, not as a value.

        A department of "  " would count as one in the per-department adoption
        figure both FRSs ask for, and look identical to a blank on screen.
        Append-only means this cannot be cleaned up afterwards.
        """
        material, plant = in_scope_material
        row = _row(_open(material, plant, department=blank, requestedFor=blank)["sessionId"])

        assert row.department is None
        assert row.requested_for is None

    def test_surrounding_space_is_trimmed(self, in_scope_material) -> None:
        material, plant = in_scope_material
        row = _row(
            _open(material, plant, department="  Concentrator  ", requestedFor="  T. Mokoena  ")[
                "sessionId"
            ]
        )

        assert row.department == "Concentrator"
        assert row.requested_for == "T. Mokoena"

    @pytest.mark.parametrize(
        "field,length",
        [("department", 64), ("requestedFor", 128)],
    )
    def test_an_over_length_value_is_refused(self, in_scope_material, field, length) -> None:
        """422 at the boundary, rather than a truncation the database performs.

        Silently storing the first 64 characters of somebody's answer into an
        append-only table is worse than refusing it: the refusal can be fixed,
        the truncation cannot.
        """
        material, plant = in_scope_material
        response = client.post(
            "/api/assistant/sessions",
            headers=OPERATOR,
            json={"materialId": material, "plant": plant, field: "x" * (length + 1)},
        )

        assert response.status_code == 422, response.text


class TestQuantityIsNoLongerCollected:
    def test_a_session_opened_today_records_no_quantity(self, in_scope_material) -> None:
        """NULL, which has always meant "not stated" here and never zero."""
        material, plant = in_scope_material

        assert _row(_open(material, plant)["sessionId"]).requested_quantity is None

    def test_the_request_model_no_longer_accepts_one(self) -> None:
        """The field is gone from the contract, not merely ignored by the caller.

        Worth asserting directly: Pydantic ignores unknown fields by default, so
        a request that still sends a quantity gets a 200 and no complaint. A test
        that only posted one would keep passing while proving nothing.
        """
        from app.api.assistant.schemas import StartSessionRequest

        assert "quantity" not in StartSessionRequest.model_fields
        assert "requestedFor" in StartSessionRequest.model_json_schema()["properties"]


class TestTheNarrativeReachesTheLiveScreen:
    """Gap B: it was written, stored, and never served to the conversation.

    ``StartSessionResponse`` had no narrative field, so the only place a
    model-written sentence ever appeared was the trace -- read afterwards, by
    whoever audits the record, and not by the person the sentence was written
    for.

    These tests assert the field exists and carries its provenance. They do not
    assert a narrative is *present*: the layer is off by default and must stay
    that way until sign-off, so on a default configuration null is the correct
    answer and asserting otherwise would fail for the right reason.
    """

    def test_the_start_response_carries_the_field(self, in_scope_material) -> None:
        material, plant = in_scope_material
        assert "narrative" in _open(material, plant)

    def test_a_served_narrative_carries_its_provenance(self, in_scope_material) -> None:
        """"The model said so" is not acceptable provenance on an audited
        programme, so the prompt and deployment travel with the text."""
        material, plant = in_scope_material
        narrative = _open(material, plant).get("narrative")

        if narrative is None:
            pytest.skip("narrative layer is off or unconfigured -- the default")

        assert narrative["text"].strip()
        assert narrative["promptId"]
        assert narrative["promptVersion"] is not None
        assert narrative["model"]

    def test_what_is_served_is_what_was_stored(self, in_scope_material) -> None:
        """The requester and the audit record must see the same sentence.

        Serving from memory while storing separately would let the two drift,
        and the stored one is what somebody is asked about months later.
        """
        material, plant = in_scope_material
        body = _open(material, plant)

        with get_sessionmaker()() as db:
            stored = db.execute(
                text("SELECT narrative FROM assistant_session WHERE id = :id"),
                {"id": body["sessionId"]},
            ).scalar_one()

        served = body.get("narrative")
        assert (served["text"] if served else None) == stored
