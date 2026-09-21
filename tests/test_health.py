"""Smoke tests for the backend template."""

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


def test_pr_event_is_accepted() -> None:
    response = client.post("/api/events/pr", json={"prNumber": "10012345", "plant": "1300"})

    assert response.status_code == 202
    assert response.json() == {"status": "received"}


def test_pr_event_rejects_non_object_body() -> None:
    """FastAPI's default validation behaviour is enough here."""
    response = client.post("/api/events/pr", json=["not", "an", "object"])

    assert response.status_code == 422
