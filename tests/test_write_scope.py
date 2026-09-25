"""The append-only write paths refuse what they could never take back.

``justification`` and ``assistant_session`` are append-only, like
``i8_attestation``: a row that gets in stays in. So a plant the platform does
not serve (ruling of 21-Sep-2026: 1300 and 1500 only), a new-purchase
justification for a part that is not repairable, or a value wider than its
column must be refused at the door with a 422 that says which rule failed --
not stored, and not bounced off the database as a 500.

Every request here is one that must be REFUSED, so none of them writes a row.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.i8_support import needs_db

client = TestClient(app)

pytestmark = needs_db

JUSTIFICATIONS = "/api/justifications"
SESSIONS = "/api/assistant/sessions"


def justification(**overrides) -> dict:
    body = {
        "kind": "NEW_ACQUISITION",
        "reasonCategory": "OTHER",
        "freeText": "Needed for a breakdown.",
        "materialId": "8000004665",
        "plant": "1300",
    }
    body.update(overrides)
    return body


class TestJustificationScope:
    def test_an_out_of_scope_plant_is_refused(self) -> None:
        response = client.post(JUSTIFICATIONS, json=justification(plant="1600"))
        assert response.status_code == 422
        assert "1300" in response.json()["detail"]

    @pytest.mark.parametrize("kind", ["QUANTITY_OVERRIDE", "PLAN_BREACH", "NO_PLAN"])
    def test_the_plant_rule_applies_to_every_kind(self, kind) -> None:
        response = client.post(
            JUSTIFICATIONS, json=justification(kind=kind, materialId="1000000123", plant="2000")
        )
        assert response.status_code == 422

    def test_a_new_purchase_of_a_non_repairable_part_is_refused(self) -> None:
        """NEW_ACQUISITION answers the I08 challenge, which only 80-series
        parts receive."""
        response = client.post(JUSTIFICATIONS, json=justification(materialId="1000000123"))
        assert response.status_code == 422
        assert "80-series" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("field", "length"),
        [("materialId", 41), ("plant", 9), ("reasonCategory", 65), ("exceptionId", 121)],
    )
    def test_a_value_wider_than_its_column_is_a_422(self, field, length) -> None:
        response = client.post(JUSTIFICATIONS, json=justification(**{field: "9" * length}))
        assert response.status_code == 422


class TestSessionScope:
    def test_a_session_for_an_out_of_scope_plant_is_not_minted(self) -> None:
        """Before this, an 80-series part at plant 1600 got an I08 session and
        the advice "nothing here suggests holding off" -- about a plant the
        platform does not count."""
        response = client.post(SESSIONS, json={"materialId": "8000004665", "plant": "1600"})
        assert response.status_code == 422
        assert "1600" in response.json()["detail"]

    @pytest.mark.parametrize(("field", "length"), [("materialId", 41), ("plant", 9)])
    def test_a_value_wider_than_its_column_is_a_422(self, field, length) -> None:
        body = {"materialId": "8000004665", "plant": "1300", field: "9" * length}
        assert client.post(SESSIONS, json=body).status_code == 422
