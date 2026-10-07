"""Suite-wide pins for the SAP system the tests describe.

The unit tests assert facts measured on SAP DEV -- known_conditions.py, the
contract, the delta evidence -- so they read DEV's discovery snapshot whatever
CPI_PATH a developer's .env points at. An exported SAP_DISCOVERY_DIR still
wins: setdefault, not assignment, so a deliberate run against another
snapshot stays possible.

And no test writes the database's SAP-system record by accident: the guard in
app.ingest.sap_system is a no-op unless a test asks for the real one with
``@pytest.mark.sap_system_guard``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault(
    "SAP_DISCOVERY_DIR",
    str(Path(__file__).resolve().parents[1] / "data-generator" / "discovery"),
)


@pytest.fixture(autouse=True)
def _one_sap_system(request, monkeypatch):
    if request.node.get_closest_marker("sap_system_guard"):
        return
    from app.ingest import sap_system

    monkeypatch.setattr(sap_system, "ensure", lambda: None)
