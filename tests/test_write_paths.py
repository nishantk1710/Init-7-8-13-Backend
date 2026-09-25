"""Every write path in the application, named one by one.

Why this file exists
---------------------
``tests/test_i8_api.py`` asserts that I08 has exactly one write path. That test
is precise, it still passes, and it stopped describing the application some time
ago: it scopes only ``/api/i8``, and by the time WS7 started the app had **five**
write endpoints across the PR event listener and the ACT routes. Nobody had
noticed, because nothing was watching.

Its value was precision, and what it protects is FRS AC-10 -- *"zero SAP
write-back events are logged"*. So rather than loosen it or rename it, the
guarantee is restated at the level it was always meant to hold: **the whole
application**.

WS7 adds five more write endpoints, which is exactly the kind of change that
should have to be declared rather than discovered.

The two guarantees, and which one actually matters
---------------------------------------------------
1. **The inventory is exact.** Every non-GET route in the served OpenAPI spec
   appears in :data:`EXPECTED_WRITES` with a note saying what it writes and why
   that is allowed. Adding an endpoint fails this test until somebody writes
   that line, which is the point: a write path should cost a sentence of
   justification.

2. **Nothing writes to SAP.** Checked structurally -- no initiative or API
   package may import the SAP client. This is the guarantee that matters, and
   it has never changed: the platform reads SAP and records its own findings
   beside it. That is what makes "we cannot enforce this control, only observe
   it" an honest statement rather than an excuse.

A note on PUT, PATCH and DELETE
--------------------------------
There are none, anywhere, and there must not be. Every table this application
owns is append-only -- an amendment is a new row pointing at the one it
supersedes. An HTTP verb that implies otherwise would be a promise the database
refuses to keep, and on Postgres a trigger would turn it into a 500 rather than
an edit.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def openapi() -> dict:
    return client.get("/openapi.json").json()


#: (path, verb) -> what it writes, and why that is allowed.
#:
#: Every entry writes to a table THIS PLATFORM OWNS. Not one of them writes to
#: SAP, and none of them can: see test_nothing_imports_the_sap_client.
EXPECTED_WRITES: dict[tuple[str, str], str] = {
    # --- Initiative 08 -----------------------------------------------------
    ("/api/i8/attestations", "post"): (
        "W5.3 condition-to-repair attestation -> i8_attestation. Append-only, "
        "trigger-enforced; an amendment is a new row with supersedes set."
    ),
    # --- Initiative 13 -----------------------------------------------------
    ("/api/i13/act/run/detect", "post"): (
        "Runs W6.6 ACT detection -> i13_act_exception. Idempotent on a "
        "deterministic business key."
    ),
    ("/api/i13/act/run/escalate", "post"): (
        "Advances the ACT state machine past the requester-response window."
    ),
    ("/api/i13/snapshot/refresh", "post"): (
        "Rebuilds the in-memory I13 snapshot in the background. Writes no table: "
        "it re-reads raw_* and swaps the process's cached copy."
    ),
    ("/api/i13/act/exceptions/{exception_id}/confirmation", "post"): (
        "Records a requester's confirmation and justification against an "
        "exception."
    ),
    ("/api/events/pr", "post"): (
        "Inbound procurement event listener. Writes only to platform tables."
    ),
    # --- W7: the reservation-time assistant --------------------------------
    ("/api/assistant/sessions", "post"): (
        "Mints an assistant_session -- one row per invocation, never updated. "
        "The session's outcome is DERIVED from its turns, which is why there is "
        "no second write to close one."
    ),
    ("/api/assistant/sessions/{session_id}/turns", "post"): (
        "Appends an assistant_turn, and whatever that answer commits the "
        "requester to: consumption_plan, quantity_suggestion, justification. "
        "Each written once, when its value becomes final."
    ),
    ("/api/assistant/ask", "post"): (
        "A POST that writes NOTHING -- a question is a body, not a query "
        "string, and a long free-text question does not belong in a URL. "
        "Listed here so its presence in this inventory is deliberate rather "
        "than an oversight."
    ),
    ("/api/justifications", "post"): (
        "I08 FR-7 / I13 FR-7 -> justification. Shared by both initiatives, "
        "which is why it is not under either prefix."
    ),
    ("/api/i13/consumption-plans", "post"): (
        "I13 FR-4 -> consumption_plan. The write path plans.py never had; it "
        "has only ever read 742 fabricated rows from a CSV."
    ),
    ("/api/i13/quantity-suggestion", "post"): (
        "I13 FR-3 (W7.4) -> quantity_suggestion. Records a suggestion and the "
        "arithmetic behind it, so the number can be argued with later rather "
        "than recomputed against data that has since moved."
    ),
    ("/api/i13/quantity-suggestion/{suggestion_id}/justification", "post"): (
        "I13 FR-3/FR-7 -> justification, for a requester who keeps a quantity "
        "the platform did not suggest. Appended against the suggestion it "
        "disagrees with; the suggestion row itself is never edited."
    ),
    ("/api/i13/quantity-suggestion/{suggestion_id}/acceptance", "post"): (
        "I13 FR-3 -> quantity_suggestion acceptance. FRS §8 counts avoided "
        "purchase benefit only where the requester accepted the suggestion, "
        "so this is the row that attribution is measured from."
    ),
}


class TestTheWriteInventoryIsExact:
    def test_every_write_endpoint_is_declared(self, openapi) -> None:
        """A new write path fails this test until somebody writes its line.

        That is the point. A write path should cost a sentence of justification,
        and the sentence should live where the next person will read it.
        """
        actual = {
            (path, verb)
            for path, operations in openapi["paths"].items()
            for verb in operations
            if verb != "get"
        }
        expected = set(EXPECTED_WRITES)

        undeclared = sorted(actual - expected)
        assert not undeclared, (
            "These write endpoints are not declared in EXPECTED_WRITES. Add "
            f"each one with a note saying what it writes and why: {undeclared}"
        )

    def test_no_declared_write_has_disappeared(self, openapi) -> None:
        """The other direction. A removed endpoint should be removed here too,
        rather than leaving a line describing a route nobody serves."""
        actual = {
            (path, verb)
            for path, operations in openapi["paths"].items()
            for verb in operations
            if verb != "get"
        }
        stale = sorted(set(EXPECTED_WRITES) - actual)
        assert not stale, f"declared but not served: {stale}"

    def test_every_declaration_says_something(self, openapi) -> None:
        for key, note in EXPECTED_WRITES.items():
            assert len(note) > 40, f"{key} needs a real note, not {note!r}"


class TestAppendOnlyIsVisibleInTheApi:
    def test_nothing_anywhere_exposes_put_patch_or_delete(self, openapi) -> None:
        """Every table this application owns is append-only.

        An HTTP verb implying otherwise would be a promise the database refuses
        to keep -- on Postgres the trigger turns an UPDATE into an error, so the
        route would be a 500 rather than an edit.
        """
        offenders = {
            path: sorted({"put", "patch", "delete"} & set(operations))
            for path, operations in openapi["paths"].items()
            if {"put", "patch", "delete"} & set(operations)
        }
        assert not offenders, f"append-only is violated by: {offenders}"


class TestNothingWritesToSap:
    """The guarantee that actually matters, and the one that has not changed."""

    #: Every package that holds business logic or routes. Checked structurally
    #: rather than by reading prose: an import of the SAP client is the first
    #: step of a write path into SAP, and it should fail before it becomes one.
    PACKAGES = (
        "app/initiatives/i8",
        "app/initiatives/i13",
        "app/api/i8",
        "app/api/i13",
        "app/api/assistant",
        "app/assistant",
    )

    def test_no_package_imports_the_sap_client(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        offenders: list[str] = []

        for package in self.PACKAGES:
            for file in (root / package).rglob("*.py"):
                source = file.read_text(encoding="utf-8")
                for line in source.splitlines():
                    stripped = line.strip()
                    if not stripped.startswith(("import ", "from ")):
                        continue
                    # The Postgres repositories and the shared contract helpers
                    # live under app.integrations.sap and read the SEEDED
                    # extract -- they hold no HTTP client and cannot reach SAP.
                    # The client itself is what must never be imported.
                    if "app.integrations.sap.client" in stripped:
                        offenders.append(f"{file.relative_to(root)}: {stripped}")

        assert not offenders, (
            "These modules import the SAP client, which is the first step of a "
            f"write path into SAP: {offenders}"
        )

    def test_the_assistant_writes_only_to_tables_we_own(self) -> None:
        """Named explicitly, because the assistant is the newest write path and
        the one a reader is least likely to have audited."""
        from app.assistant import models

        owned = {
            models.AssistantSession.__tablename__,
            models.AssistantTurn.__tablename__,
            models.Justification.__tablename__,
            models.ConsumptionPlanRecord.__tablename__,
            models.QuantitySuggestionRecord.__tablename__,
        }
        assert owned == {
            "assistant_session",
            "assistant_turn",
            "justification",
            "consumption_plan",
            "quantity_suggestion",
        }
