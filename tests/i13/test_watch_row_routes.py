"""The two routes that read one WATCH row work on either snapshot store.

``/quantity-suggestion/compute`` and ``POST /quantity-suggestion`` read
``snapshot.watch.get(...)``. That is a mapping on the in-memory ``I13Snapshot``
but a paged query on ``SqlSnapshot`` -- so with ``I13_SNAPSHOT_STORE=sql`` both
answered 500 (``'function' object has no attribute 'get'``). They now call
``watch_row``, which both stores answer.

Served here from a stand-in with ``SqlSnapshot``'s surface (``watch`` a method,
``watch_row`` the lookup), so a return to ``.watch.get`` fails these tests.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api.i13.deps import snapshot_or_live
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from tests.assistant.conftest import watch_metric

API = f"{get_settings().api_prefix}/i13"


class StoredSnapshot:
    def __init__(self, rows):
        self._rows = rows

    def watch(self, **filters):
        raise AssertionError("one row is read with watch_row, not by paging WATCH")

    def watch_row(self, material, plant):
        return self._rows.get((material, plant))


@pytest.fixture
def serve(monkeypatch):
    """Serve ``rows`` from a stored-style snapshot, with no database."""

    def install(rows):
        app.dependency_overrides[snapshot_or_live] = lambda: StoredSnapshot(rows)
        app.dependency_overrides[get_db] = lambda: None
        # The I13 response headers report the served version; not under test.
        monkeypatch.setattr("app.initiatives.i13.snapshot.served_info", lambda: None)
        return TestClient(app)

    yield install
    app.dependency_overrides.pop(snapshot_or_live, None)
    app.dependency_overrides.pop(get_db, None)


def test_compute_reads_the_stored_row(serve):
    client = serve({("1000000123", "1300"): watch_metric(stock_on_hand=Decimal("4"))})

    response = client.get(
        f"{API}/quantity-suggestion/compute", params={"material": "1000000123", "plant": "1300", "quantity": "5"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["material"] == "1000000123" and body["requestedQuantity"] == "5"


def test_compute_without_a_row_is_404_not_500(serve):
    client = serve({})

    response = client.get(
        f"{API}/quantity-suggestion/compute", params={"material": "1000000999", "plant": "1300", "quantity": "5"}
    )

    assert response.status_code == 404, response.text


def test_create_without_a_row_is_404_not_500(serve):
    client = serve({})

    response = client.post(
        f"{API}/quantity-suggestion",
        json={"material": "1000000999", "plant": "1300", "requested_quantity": "5", "plan_window_months": "6"},
    )

    assert response.status_code == 404, response.text
    assert "WATCH row" in response.json()["detail"]
