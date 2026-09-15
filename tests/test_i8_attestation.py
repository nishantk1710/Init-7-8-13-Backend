"""W5.3 -- the condition-to-repair attestation, its exception, and the queue.

Three tiers, the same shape the rest of the I08 suite uses:

* rules that need nothing -- validation, the matching window, status derivation
* the write path, which needs a database but not the extracts
* coverage and the queues, which need the seeded register

The tests that matter most here are the ones about what must NOT happen: a
second attestation must not overwrite the first, an amendment must not edit the
original, and the exception must not clear for a line just outside the window.
An audit record that can be quietly rewritten is not an audit record, and a
boundary that is only tested in the middle is not tested.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.initiatives.i8.attestation import (
    CONDITION_LABELS,
    AttestationCoverage,
    AttestationDraft,
    AttestationError,
    Recommendation,
    _within_window,
    coverage,
    find,
    record,
    validate,
)
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.declarations import STATUS_NOTES, build_queue
from app.initiatives.i8.exceptions import (
    RAISED_BY_I8,
    ExceptionType,
    build_exceptions,
)
from app.main import app
from tests.i8_support import needs_db, needs_views

client = TestClient(app)

ATTESTATIONS = "/api/i8/attestations"
DECLARATIONS = "/api/i8/declarations"
EXCEPTIONS = "/api/i8/exceptions"


def a_draft(**overrides) -> AttestationDraft:
    base = dict(
        material_id="8000005632",
        plant="1300",
        quantity=Decimal(1),
        condition_description="Bearing seized, housing intact.",
        fault_category="BEARING_FAILURE",
        recommendation="REPAIRABLE",
    )
    base.update(overrides)
    return AttestationDraft(**base)


class FakeLine:
    """The three fields coverage() reads off a repair line.

    A stand-in rather than a real RepairLine so the window rule can be tested
    without a database or a 40-field constructor.
    """

    def __init__(self, document, item, material_id, plant, raised_at):
        self.purchasing_document = document
        self.item = item
        self.material_id = material_id
        self.plant = plant
        self.raised_at = raised_at
        self.description = "A part"
        self.received_at = None
        self.pr_number = "PR1"
        self.pr_item = "10"
        self.requisitioner = "10316"

    @property
    def is_open(self) -> bool:
        return self.received_at is None

    @property
    def key(self):
        return (self.purchasing_document, self.item)


# --- Validation, no database ----------------------------------------------


class TestValidation:
    def test_a_good_draft_passes_and_comes_back_normalised(self) -> None:
        clean = validate(a_draft(material_id="000000008000005632"))
        # Ruling 5.1: never compare two raw material numbers.
        assert clean.material_id == "8000005632"

    def test_lowercase_input_is_accepted_and_upper_cased(self) -> None:
        clean = validate(a_draft(fault_category="wear", recommendation="scrap"))
        assert clean.fault_category == "WEAR"
        assert clean.recommendation == "SCRAP"

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"material_id": "   "}, "materialId"),
            ({"material_id": "0000"}, "materialId"),  # normalises to nothing
            ({"plant": ""}, "plant"),
            ({"quantity": Decimal(0)}, "quantity"),
            ({"quantity": Decimal(-1)}, "quantity"),
            ({"condition_description": "   "}, "conditionDescription"),
            ({"fault_category": "NOT_A_CATEGORY"}, "faultCategory"),
            ({"recommendation": "PROBABLY"}, "recommendation"),
        ],
    )
    def test_rejects(self, overrides, expected) -> None:
        with pytest.raises(AttestationError, match=expected):
            validate(a_draft(**overrides))

    def test_the_fault_category_list_comes_from_config_not_code(self) -> None:
        """The list is VZI's vocabulary and will change. It must be one .env
        line, not a code change -- the same rule the repair conventions follow."""
        cfg = get_i8_settings()
        assert "BEARING_FAILURE" in cfg.fault_category_list
        # Nothing validates against a literal list defined in the module.
        import app.initiatives.i8.attestation as module

        source = module.__file__
        with open(source, encoding="utf-8") as handle:
            body = handle.read()
        assert "BEARING_FAILURE" not in body, (
            "a fault category is hard-coded in attestation.py -- it belongs in config"
        )

    def test_every_recommendation_maps_to_a_frontend_condition(self) -> None:
        """Checked against src/features/initiative-8/types/repair.ts."""
        assert set(CONDITION_LABELS) == set(Recommendation)
        assert set(CONDITION_LABELS.values()) == {
            "Repairable",
            "Beyond Economical Repair",
            "Scrap",
        }


# --- The matching window ---------------------------------------------------


class TestTheMatchingWindow:
    """The boundary, not just the middle.

    The window is a PROPOSAL (open question 3) -- material + plant + a date
    range is the only key both sides share. So the edges are tested explicitly:
    a rule nobody has confirmed should at least behave exactly as described.
    """

    RAISED = date(2026, 5, 1)
    WINDOW = timedelta(days=30)

    def _at(self, day: date) -> datetime:
        return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)

    def test_same_day_matches(self) -> None:
        assert _within_window(self._at(self.RAISED), self.RAISED, self.WINDOW)

    def test_the_last_day_inside_the_window_matches(self) -> None:
        assert _within_window(
            self._at(self.RAISED - self.WINDOW), self.RAISED, self.WINDOW
        )
        assert _within_window(
            self._at(self.RAISED + self.WINDOW), self.RAISED, self.WINDOW
        )

    def test_one_day_outside_the_window_does_not(self) -> None:
        """The test the plan asks for by name: a line just outside still raises."""
        assert not _within_window(
            self._at(self.RAISED - self.WINDOW - timedelta(days=1)),
            self.RAISED,
            self.WINDOW,
        )
        assert not _within_window(
            self._at(self.RAISED + self.WINDOW + timedelta(days=1)),
            self.RAISED,
            self.WINDOW,
        )

    def test_a_line_with_no_raised_date_is_never_silently_uncoverable(self) -> None:
        """ERDAT is populated on all 1,225 repair lines, so this is a guard.

        It matters anyway: "no date" must not mean "matches nothing", which
        would raise an exception that nobody could ever clear.
        """
        assert _within_window(self._at(date(2020, 1, 1)), None, self.WINDOW)


# --- The declaration queue, no database ------------------------------------


class TestDeclarationQueue:
    def _coverage(self, covered=None, uncovered=None) -> AttestationCoverage:
        return AttestationCoverage(
            covered=covered or {},
            uncovered=uncovered or set(),
            window_days=30,
        )

    def test_no_attestation_reads_as_required(self) -> None:
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        rows = build_queue([line], self._coverage(uncovered={line.key}))
        assert rows[0].status == "Required"
        assert rows[0].condition is None
        assert rows[0].declared_by is None
        assert rows[0].is_outstanding

    def test_a_repairable_attestation_reads_as_completed(self, fake_attestation) -> None:
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        rows = build_queue(
            [line], self._coverage(covered={line.key: fake_attestation("REPAIRABLE")})
        )
        assert rows[0].status == "Completed"
        assert rows[0].condition == "Repairable"
        assert not rows[0].is_outstanding

    @pytest.mark.parametrize(
        "recommendation", ["BEYOND_ECONOMICAL_REPAIR", "SCRAP"]
    )
    def test_assessed_but_not_repairable_reads_as_flagged(
        self, fake_attestation, recommendation
    ) -> None:
        """The case the queue exists for: somebody looked, said do not repair
        this, and it went for repair anyway. That is a finding, not a gap."""
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        rows = build_queue(
            [line], self._coverage(covered={line.key: fake_attestation(recommendation)})
        )
        assert rows[0].status == "Flagged"
        assert rows[0].is_outstanding
        assert rows[0].condition == CONDITION_LABELS[Recommendation(recommendation)]

    def test_pending_is_never_emitted(self, fake_attestation) -> None:
        """It means "submitted, awaiting sign-off" and there is no such state.

        Emitting it would invent a stage of a process nobody operates. It stays
        in the type for the day a review step is actually built.
        """
        lines = [
            FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1)),
            FakeLine("4500", "20", "8000005633", "1300", date(2026, 5, 1)),
        ]
        cover = self._coverage(
            covered={lines[0].key: fake_attestation("REPAIRABLE")},
            uncovered={lines[1].key},
        )
        assert {r.status for r in build_queue(lines, cover)} == {"Completed", "Required"}
        assert "Pending" in STATUS_NOTES  # documented, deliberately unused

    def test_source_is_null_rather_than_guessed(self) -> None:
        """EBAN covers 521 of 1,201 repair requisitions and every one reads 'F'
        -- created from an order, which is neither Manual nor MRP-generated.
        Both labels are false for every row, so neither is sent."""
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        rows = build_queue([line], self._coverage(uncovered={line.key}))
        assert rows[0].source is None

    def test_requester_is_carried_through_as_a_code(self) -> None:
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        rows = build_queue([line], self._coverage(uncovered={line.key}))
        assert rows[0].requester == "10316"


# --- The exception ---------------------------------------------------------


class TestMissingAttestationException:
    def test_an_uncovered_line_raises_it(self) -> None:
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        cover = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, stats = build_exceptions([line], cover)

        assert len(items) == 1
        assert items[0].type == ExceptionType.MISSING_ATTESTATION.value
        assert stats.by_type == {"MISSING_ATTESTATION": 1}

    def test_a_covered_line_does_not(self, fake_attestation) -> None:
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        cover = AttestationCoverage(
            covered={line.key: fake_attestation("REPAIRABLE")},
            uncovered=set(),
            window_days=30,
        )
        items, stats = build_exceptions([line], cover)
        assert items == []
        assert stats.total == 0
        assert stats.lines_covered == 1

    def test_the_detail_says_what_was_searched_for(self) -> None:
        """So a reader can tell a real gap from a matching rule that did not fit."""
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        cover = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        detail = build_exceptions([line], cover)[0][0].detail
        assert "8000005632" in detail and "1300" in detail and "30 days" in detail

    def test_an_open_line_is_more_severe_than_a_closed_one(self) -> None:
        """An open line is a part out there now with no assessment, which
        somebody can still act on. A closed one can only be counted."""
        open_line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        closed = FakeLine("4500", "20", "8000005633", "1300", date(2026, 5, 1))
        closed.received_at = date(2026, 6, 1)
        cover = AttestationCoverage(
            covered={}, uncovered={open_line.key, closed.key}, window_days=30
        )
        items, _stats = build_exceptions([open_line, closed], cover)
        by_doc = {i.item: i.severity for i in items}
        assert by_doc["10"] == "warning"
        assert by_doc["20"] == "info"

    def test_the_type_column_is_open_for_the_ones_we_do_not_raise(self) -> None:
        """MISSING_SESSION_ID and UNJUSTIFIED_ACQUISITION are FR-5/7/8. They are
        declared so adding them later is a detector, not a migration -- and the
        API says which are actually raised, so an empty count is distinguishable
        from an unimplemented check."""
        assert {t.value for t in ExceptionType} > {t.value for t in RAISED_BY_I8}
        assert RAISED_BY_I8 == frozenset({ExceptionType.MISSING_ATTESTATION})

    def test_a_line_with_no_plant_is_not_reported(self) -> None:
        """The material-plant key cannot be formed, so an exception for it could
        never be cleared by anyone. That is noise, not a finding."""
        line = FakeLine("4500", "10", "8000005632", None, date(2026, 5, 1))
        cover = AttestationCoverage(covered={}, uncovered=set(), window_days=30)
        assert build_exceptions([line], cover)[0] == []


@pytest.fixture
def fake_attestation():
    """A stored attestation, without a database."""
    from app.initiatives.i8.models import RepairAttestation

    def make(recommendation: str) -> RepairAttestation:
        return RepairAttestation(
            id="ATT-TEST",
            material_id="8000005632",
            plant="1300",
            quantity=Decimal(1),
            condition_description="Assessed.",
            fault_category="WEAR",
            recommendation=recommendation,
            attestor="tester",
            attested_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )

    return make


# --- The write path, needs a database --------------------------------------


@needs_db
class TestTheWritePath:
    @pytest.fixture(autouse=True)
    def clean_table(self):
        """Empty the table around each test.

        DELETE is blocked by the immutability trigger -- correctly -- so this
        disables it, which is exactly the privilege no application code has.
        """
        from app.core.db import get_sessionmaker

        def wipe():
            with get_sessionmaker()() as db:
                db.execute(
                    text(
                        "ALTER TABLE i8_attestation DISABLE TRIGGER "
                        "i8_attestation_no_update_or_delete"
                    )
                )
                db.execute(text("DELETE FROM i8_attestation"))
                db.execute(
                    text(
                        "ALTER TABLE i8_attestation ENABLE TRIGGER "
                        "i8_attestation_no_update_or_delete"
                    )
                )
                db.commit()

        wipe()
        yield
        wipe()

    @pytest.fixture
    def db(self):
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as session:
            yield session

    def test_record_stores_and_returns_it(self, db) -> None:
        stored = record(db, a_draft(), attestor="tester")
        assert stored.id.startswith("ATT-")
        assert stored.material_id == "8000005632"
        assert stored.attestor == "tester"
        assert stored.session_id is None

    def test_the_timestamp_is_server_set(self, db) -> None:
        """The attestor does not get to choose when they say they looked."""
        before = datetime.now(timezone.utc)
        stored = record(db, a_draft(), attestor="tester")
        assert before <= stored.attested_at <= datetime.now(timezone.utc)

    def test_a_second_attestation_does_not_overwrite_the_first(self, db) -> None:
        """The plan names this one. Two assessments of the same part are two
        records, not one record edited twice."""
        first = record(db, a_draft(), attestor="one")
        second = record(
            db, a_draft(condition_description="A different view."), attestor="two"
        )

        assert first.id != second.id
        rows = find(db, material_id="8000005632")
        assert len(rows) == 2
        assert {r.attestor for r in rows} == {"one", "two"}
        # The original is byte-for-byte what it was.
        assert [r for r in rows if r.id == first.id][0].condition_description == (
            "Bearing seized, housing intact."
        )

    def test_an_amendment_chains_and_leaves_the_original_readable(self, db) -> None:
        original = record(db, a_draft(), attestor="one")
        amendment = record(
            db,
            a_draft(
                recommendation="SCRAP",
                condition_description="On strip-down, casing cracked.",
                supersedes=original.id,
            ),
            attestor="two",
        )

        assert amendment.supersedes == original.id

        rows = {r.id: r for r in find(db, material_id="8000005632")}
        assert len(rows) == 2
        assert rows[original.id].recommendation == "REPAIRABLE"
        assert rows[original.id].condition_description == "Bearing seized, housing intact."

        current = find(db, material_id="8000005632", include_superseded=False)
        assert [r.id for r in current] == [amendment.id]

    def test_an_amendment_must_point_at_something(self, db) -> None:
        with pytest.raises(AttestationError, match="does not exist"):
            record(db, a_draft(supersedes="ATT-NOPE"), attestor="tester")

    def test_an_amendment_cannot_move_the_part(self, db) -> None:
        """Otherwise the chain would read as one part's history when it is two."""
        original = record(db, a_draft(), attestor="one")
        with pytest.raises(AttestationError, match="same material and plant"):
            record(
                db,
                a_draft(material_id="8000009999", supersedes=original.id),
                attestor="two",
            )

    def test_the_database_refuses_an_update(self, db) -> None:
        """The service layer never issues one. This proves the floor beneath it."""
        stored = record(db, a_draft(), attestor="tester")
        with pytest.raises(Exception, match="append-only"):
            db.execute(
                text("UPDATE i8_attestation SET attestor = 'x' WHERE id = :id"),
                {"id": stored.id},
            )
        db.rollback()

    def test_the_database_refuses_a_delete(self, db) -> None:
        stored = record(db, a_draft(), attestor="tester")
        with pytest.raises(Exception, match="append-only"):
            db.execute(
                text("DELETE FROM i8_attestation WHERE id = :id"), {"id": stored.id}
            )
        db.rollback()

    def test_find_normalises_both_sides(self, db) -> None:
        record(db, a_draft(material_id="8000005632"), attestor="tester")
        assert len(find(db, material_id="000000008000005632")) == 1

    def test_coverage_matches_on_material_plant_and_window(self, db) -> None:
        record(db, a_draft(), attestor="tester")
        today = datetime.now(timezone.utc).date()

        inside = FakeLine("4500", "10", "8000005632", "1300", today)
        outside = FakeLine("4500", "20", "8000005632", "1300", today - timedelta(days=400))
        wrong_plant = FakeLine("4500", "30", "8000005632", "1500", today)

        cover = coverage(db, [inside, outside, wrong_plant])
        assert inside.key in cover.covered
        assert outside.key in cover.uncovered
        assert wrong_plant.key in cover.uncovered
        assert cover.window_days == get_i8_settings().attestation_window_days


# --- The API ---------------------------------------------------------------


@needs_db
class TestTheApi:
    @pytest.fixture(autouse=True)
    def clean_table(self):
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.service import reset_attestation_view

        def wipe():
            with get_sessionmaker()() as db:
                db.execute(
                    text(
                        "ALTER TABLE i8_attestation DISABLE TRIGGER "
                        "i8_attestation_no_update_or_delete"
                    )
                )
                db.execute(text("DELETE FROM i8_attestation"))
                db.execute(
                    text(
                        "ALTER TABLE i8_attestation ENABLE TRIGGER "
                        "i8_attestation_no_update_or_delete"
                    )
                )
                db.commit()
            reset_attestation_view()

        wipe()
        yield
        wipe()

    def body(self, **overrides) -> dict:
        payload = {
            "materialId": "8000005632",
            "plant": "1300",
            "quantity": 1,
            "conditionDescription": "Bearing seized, housing intact.",
            "faultCategory": "BEARING_FAILURE",
            "recommendation": "REPAIRABLE",
        }
        payload.update(overrides)
        return payload

    def test_post_returns_201_and_the_row_comes_back_on_get(self) -> None:
        created = client.post(ATTESTATIONS, json=self.body())
        assert created.status_code == 201

        listed = client.get(f"{ATTESTATIONS}?materialId=8000005632").json()
        assert listed["total"] == 1
        assert listed["items"][0]["id"] == created.json()["id"]

    def test_the_response_carries_the_frontend_condition_wording(self) -> None:
        created = client.post(ATTESTATIONS, json=self.body()).json()
        assert created["condition"] == "Repairable"

    def test_the_fault_category_list_is_served_with_the_data(self) -> None:
        """So a form does not hard-code VZI's vocabulary."""
        listed = client.get(ATTESTATIONS).json()
        assert "BEARING_FAILURE" in listed["faultCategories"]

    def test_an_unknown_fault_category_is_422_and_says_what_is_allowed(self) -> None:
        response = client.post(ATTESTATIONS, json=self.body(faultCategory="MADE_UP"))
        assert response.status_code == 422
        assert "BEARING_FAILURE" in response.json()["detail"]

    def test_the_amendment_chain_is_visible_on_the_wire(self) -> None:
        original = client.post(ATTESTATIONS, json=self.body()).json()
        amendment = client.post(
            ATTESTATIONS,
            json=self.body(recommendation="SCRAP", supersedes=original["id"]),
        ).json()

        listed = client.get(f"{ATTESTATIONS}?materialId=8000005632").json()
        rows = {r["id"]: r for r in listed["items"]}
        assert rows[original["id"]]["supersededBy"] == amendment["id"]
        assert rows[amendment["id"]]["supersedes"] == original["id"]
        # And the original is still there, unedited.
        assert rows[original["id"]]["recommendation"] == "REPAIRABLE"

    def test_current_only_hides_superseded_rows(self) -> None:
        original = client.post(ATTESTATIONS, json=self.body()).json()
        client.post(
            ATTESTATIONS, json=self.body(recommendation="SCRAP", supersedes=original["id"])
        )
        current = client.get(f"{ATTESTATIONS}?materialId=8000005632&currentOnly=true").json()
        assert current["total"] == 1
        assert current["items"][0]["id"] != original["id"]

    def test_the_attestor_is_not_taken_from_the_body(self) -> None:
        """An audit record whose author is self-declared is not an audit record."""
        created = client.post(
            ATTESTATIONS, json=self.body(attestor="somebody.else")
        ).json()
        assert created["attestor"] != "somebody.else"


@needs_views
class TestAgainstTheSeededRegister:
    """The queues over the real 1,225-line register."""

    @pytest.fixture(autouse=True)
    def clean_table(self):
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.service import reset_attestation_view

        def wipe():
            with get_sessionmaker()() as db:
                db.execute(
                    text(
                        "ALTER TABLE i8_attestation DISABLE TRIGGER "
                        "i8_attestation_no_update_or_delete"
                    )
                )
                db.execute(text("DELETE FROM i8_attestation"))
                db.execute(
                    text(
                        "ALTER TABLE i8_attestation ENABLE TRIGGER "
                        "i8_attestation_no_update_or_delete"
                    )
                )
                db.commit()
            reset_attestation_view()

        wipe()
        yield
        wipe()

    def test_every_repair_line_is_in_the_declaration_queue(self) -> None:
        meta = client.get(f"{DECLARATIONS}?pageSize=1").json()["meta"]
        assert meta["total"] == 1225

    def test_with_no_attestations_everything_is_an_exception(self) -> None:
        """**This number is the business case.** 1,225 repair lines, zero
        attestations, because the control did not exist before this platform.

        If it ever drops without somebody having recorded an attestation, the
        check has been weakened rather than satisfied.
        """
        meta = client.get(f"{EXCEPTIONS}?pageSize=1").json()["meta"]
        assert meta["total"] == 1225
        assert meta["linesChecked"] == 1225
        assert meta["linesCovered"] == 0
        assert meta["byType"] == {"MISSING_ATTESTATION": 1225}

    def test_open_lines_are_warnings_and_closed_ones_are_information(self) -> None:
        meta = client.get(f"{EXCEPTIONS}?pageSize=1").json()["meta"]
        assert meta["bySeverity"] == {"warning": 788, "info": 437}
        assert client.get(f"{EXCEPTIONS}?openOnly=true&pageSize=1").json()["total"] == 788

    def test_the_meta_counts_do_not_change_when_the_list_is_filtered(self) -> None:
        """"How much of the register is uncovered" must not move because
        somebody filtered to one plant."""
        unfiltered = client.get(f"{EXCEPTIONS}?pageSize=1").json()
        filtered = client.get(f"{EXCEPTIONS}?plant=1300&pageSize=1").json()
        assert filtered["total"] < unfiltered["total"]
        assert filtered["meta"]["total"] == unfiltered["meta"]["total"]

    def test_seeding_a_demo_attestation_clears_its_exception(self) -> None:
        """The whole loop: before, seed, after.

        Uses the demo seeder rather than the API on purpose -- the API stamps
        the server clock, and every line in this July extract was raised months
        outside the matching window of "now". That is not a bug in either one;
        see app/initiatives/i8/demo_seed.py.
        """
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.demo_seed import seed
        from app.initiatives.i8.service import reset_attestation_view

        before = client.get(f"{EXCEPTIONS}?pageSize=1").json()["meta"]["total"]

        with get_sessionmaker()() as db:
            result = seed(db, count=3)
        reset_attestation_view()

        after = client.get(f"{EXCEPTIONS}?pageSize=1").json()
        assert result.created == 3
        assert after["meta"]["total"] < before
        assert after["meta"]["linesCovered"] >= 3

        # And those lines now read as declared rather than required.
        statuses = client.get(f"{DECLARATIONS}?pageSize=1").json()["meta"]["byStatus"]
        assert statuses.get("Required", 0) < 1225
        assert sum(statuses.values()) == 1225

    def test_declaration_field_names_match_the_frontend_type(self) -> None:
        """Checked against src/features/initiative-8/types/repair.ts.

        W5.4 renders this on the same critical path, so a rename here is
        somebody else's merge conflict tomorrow.
        """
        row = client.get(f"{DECLARATIONS}?pageSize=1").json()["items"][0]
        assert {
            "id",
            "pr",
            "material",
            "requester",
            "source",
            "hasActiveRepair",
            "relatedRepairId",
            "status",
            "nextAction",
            "createdAt",
        } <= set(row)
        assert row["status"] in {"Required", "Pending", "Completed", "Flagged"}
