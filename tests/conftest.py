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

# Sign-in is off for the suite unless a test switches it on with the
# ``auth_enabled`` fixture -- a developer's .env with AUTH_ENABLED=true must not
# turn every route into a 401. setdefault again: an exported value still wins.
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("SAP_EVENTS_AUTH", "off")

# The I08 complete flow is ON for the suite (29-Oct-2026). It defaults off in
# every environment until approved, but the tests describe the flow itself;
# what it looks like switched off is tests/test_i8_feature_switch.py, which
# turns it off explicitly. Assignment, not setdefault: a developer's shell or
# .env saying false must not quietly fail every complete-flow test.
os.environ["I8_COMPLETE_FLOW_ENABLED"] = "true"

# Test tokens and the fixtures that make the API trust them; see the module.
from tests.auth_helpers import auth_enabled, sap_events_entra  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _one_sap_system(request, monkeypatch):
    if request.node.get_closest_marker("sap_system_guard"):
        return
    from app.ingest import sap_system

    monkeypatch.setattr(sap_system, "ensure", lambda: None)
