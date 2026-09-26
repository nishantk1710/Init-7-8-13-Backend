"""Smoke tests for the backend template."""

import json
import logging

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "spares-ai-backend"


def test_root_returns_service_info() -> None:
    """The bare host must not 404 -- it should say what this service is."""
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "spares-ai-backend"
    assert body["status"] == "ok"
    assert body["docs_url"] == "/docs"
    assert body["health_url"] == "/api/health"


# The payload SAP confirmed for the PR-created event.
SAP_PR_EVENT = {
    "BANFN": "1000000567",
    "CREATED_ON": "2026-09-16",
    "CREATED_BY": "VSUNEEL",
    "CREATED_AT": "13:14:03",
    "MESSAGE": "PR Created Successfully",
}


def test_pr_event_is_accepted() -> None:
    response = client.post("/api/events/pr", json={"prNumber": "10012345", "plant": "1300"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "received"
    # `stored` is the key the event was appended to. None here because no
    # STORAGE_URL is configured under test -- which must never turn an
    # accepted event into a refused one.
    assert body["stored"] is None


def test_pr_event_accepts_the_payload_sap_confirmed() -> None:
    response = client.post("/api/events/pr", json=SAP_PR_EVENT)

    assert response.status_code == 202
    assert response.json()["status"] == "received"


def test_pr_event_logs_the_payload_in_saps_own_field_names(caplog) -> None:
    """The log line is what we compare against CPI when the two disagree.

    It has to read in SAP's spelling, not our Python one, or every comparison
    needs a mental translation first.
    """
    with caplog.at_level(logging.INFO, logger="app.api.events.pr"):
        client.post("/api/events/pr", json=SAP_PR_EVENT)

    logged = " | ".join(caplog.messages)
    assert "PR event received" in logged
    for field, value in SAP_PR_EVENT.items():
        assert field in logged, f"{field} missing from the log line"
        assert value in logged, f"value of {field} missing from the log line"


def test_pr_event_survives_a_field_sap_adds_without_telling_us() -> None:
    """The iFlow is not versioned, so an unknown field must not 422."""
    response = client.post(
        "/api/events/pr",
        json={**SAP_PR_EVENT, "WERKS": "1101", "NEW_FIELD": "whatever"},
    )

    assert response.status_code == 202


def test_pr_event_survives_a_field_sap_drops() -> None:
    """Losing a real requisition to a missing field would be the worse bug."""
    response = client.post("/api/events/pr", json={"BANFN": "1000000567"})

    assert response.status_code == 202


def test_pr_event_without_banfn_is_accepted_but_warned_about(caplog) -> None:
    """Accepted, because refusing it would drop the event entirely."""
    with caplog.at_level(logging.WARNING, logger="app.api.events.pr"):
        response = client.post("/api/events/pr", json={"MESSAGE": "PR Created Successfully"})

    assert response.status_code == 202
    assert any("BANFN" in message for message in caplog.messages)


def test_pr_event_rejects_non_object_body() -> None:
    """400 with a reason, rather than FastAPI's 422 blob.

    SAP's HTTP client surfaces the status code and little else, so the reason
    has to be something a person can act on without our logs in front of them.
    """
    response = client.post("/api/events/pr", json=["not", "an", "object"])

    assert response.status_code == 400
    assert "JSON object" in response.json()["detail"]


def test_pr_event_accepts_json_sent_as_text_plain() -> None:
    """The 422 SAP hit in production.

    ABAP's cl_http_client sends text/plain unless told otherwise, and FastAPI
    parses the body before the model is consulted -- so a valid JSON event was
    refused over a header. Content type is not consulted any more.
    """
    response = client.post(
        "/api/events/pr",
        content=json.dumps(SAP_PR_EVENT),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 202


def test_pr_event_accepts_a_numeric_banfn() -> None:
    """ABAP serialisers differ on whether a NUMC-like field is quoted."""
    response = client.post("/api/events/pr", json={"BANFN": 1000000567})

    assert response.status_code == 202


def test_pr_event_accepts_abap_dats_and_tims_formats() -> None:
    """DATS is 20260916 and TIMS is 131403; neither is ISO."""
    response = client.post(
        "/api/events/pr",
        json={"BANFN": "1000000567", "CREATED_ON": "20260916", "CREATED_AT": "131403"},
    )

    assert response.status_code == 202


def test_pr_event_rejects_an_unreadable_body_with_the_reason_logged(caplog) -> None:
    """A FastAPI 422 never reached our code, which is why the live failures
    were invisible from this side and had to be guessed at from SAP's."""
    with caplog.at_level(logging.WARNING, logger="app.api.events.pr"):
        response = client.post(
            "/api/events/pr",
            content="<root><BANFN>1</BANFN></root>",
            headers={"Content-Type": "application/xml"},
        )

    assert response.status_code == 400
    logged = " | ".join(caplog.messages)
    assert "REJECTED" in logged
    assert "application/xml" in logged
