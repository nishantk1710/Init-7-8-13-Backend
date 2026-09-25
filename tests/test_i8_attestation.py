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
            # The table is append-only: each of these would be a permanent row.
            ({"material_id": "1000000123"}, "80-series"),
            ({"plant": "1600"}, "outside the platform's scope"),
            ({"quantity": Decimal(1001)}, "typo"),
        ],
    )
    def test_rejects(self, overrides, expected) -> None:
        with pytest.raises(AttestationError, match=expected):
            validate(a_draft(**overrides))

    def test_the_quantity_ceiling_is_configuration(self) -> None:
        from app.initiatives.i8.config import I8Settings

        cfg = I8Settings(_env_file=None, attestation_max_quantity=5)
        assert validate(a_draft(quantity=Decimal(5)), cfg).quantity == 5
        with pytest.raises(AttestationError, match="more than 5"):
            validate(a_draft(quantity=Decimal(6)), cfg)

    def test_a_material_the_universe_does_not_know_is_refused(self) -> None:
        known = {"8000005632"}
        assert validate(a_draft(), known_materials=known).material_id == "8000005632"
        with pytest.raises(AttestationError, match="repairable universe"):
            validate(a_draft(material_id="8099999999"), known_materials=known)

    def test_without_a_universe_the_existence_check_is_skipped(self) -> None:
        """Callers that do not hold the snapshot -- the demo seeder, the unit
        tests -- still get every other rule."""
        assert validate(a_draft(material_id="8099999999")).material_id == "8099999999"

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

    def test_a_line_raised_before_the_cutover_is_labelled_not_accused(self) -> None:
        """The 20-Sep ruling: "on them can we show before Spares Automation".

        MISSING_ATTESTATION fires on all 1,225 historical lines because the
        control did not exist when they were raised. The exception still fires
        and still counts -- what changes is that it says why.
        """
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        cover = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, stats = build_exceptions([line], cover, cutover=date(2026, 10, 1))

        assert items[0].pre_automation is True
        assert items[0].title == "Raised before Spares Automation"
        assert "2026-10-01" in items[0].detail
        # Open, but INFO: there was no form to fill in when it was raised.
        assert items[0].severity == "info"
        # Counted, but not as work.
        assert (stats.total, stats.pre_automation, stats.actionable) == (1, 1, 0)

    def test_a_line_raised_after_the_cutover_is_a_real_miss(self) -> None:
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 11, 1))
        cover = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, stats = build_exceptions([line], cover, cutover=date(2026, 10, 1))

        assert items[0].pre_automation is False
        assert items[0].title == "No condition-to-repair attestation"
        assert items[0].severity == "warning"
        assert (stats.total, stats.pre_automation, stats.actionable) == (1, 0, 1)

    def test_no_cutover_configured_changes_nothing(self) -> None:
        """The shipped default. VZI has not given the date yet, and a guessed
        one would silently forgive real misses on one side of it."""
        line = FakeLine("4500", "10", "8000005632", "1300", date(2026, 5, 1))
        cover = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, stats = build_exceptions([line], cover)

        assert items[0].pre_automation is False
        assert items[0].title == "No condition-to-repair attestation"
        assert (stats.pre_automation, stats.actionable) == (0, 1)
        assert stats.attestation_cutover_date is None

    def test_a_line_with_no_date_is_not_quietly_forgiven(self) -> None:
        """Guessing in the forgiving direction is still guessing, and the line
        it would forgive is the one we know least about."""
        line = FakeLine("4500", "10", "8000005632", "1300", None)
        cover = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, _stats = build_exceptions([line], cover, cutover=date(2026, 10, 1))
        assert items[0].pre_automation is False

    def test_the_full_number_survives_the_label(self) -> None:
        """The 1,225 figure is the business case for the initiative. The label
        is about how it reads, not about making it smaller -- so `total` still
        carries it and `actionable` is served beside it, never instead."""
        lines = [
            FakeLine("4500", str(i), "800000563" + str(i % 10), "1300", date(2026, 5, 1))
            for i in range(10)
        ]
        cover = AttestationCoverage(
            covered={}, uncovered={line.key for line in lines}, window_days=30
        )
        _items, stats = build_exceptions(lines, cover, cutover=date(2026, 10, 1))
        assert stats.total == 10
        assert stats.actionable == 0
        assert stats.attestation_cutover_date == date(2026, 10, 1)

    def test_the_type_column_is_open_for_the_ones_we_do_not_raise(self) -> None:
        """MISSING_SESSION_ID is FR-8 and ours, and is not raised until
        RESB.BEDNR is exposed. It is declared so adding it is a detector, not a
        migration -- and the API says which are actually raised, so an empty
        count is distinguishable from an unimplemented check."""
        assert {t.value for t in ExceptionType} > {t.value for t in RAISED_BY_I8}
        assert RAISED_BY_I8 == frozenset(
            {ExceptionType.MISSING_ATTESTATION, ExceptionType.UNJUSTIFIED_ACQUISITION}
        )

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

    @pytest.mark.parametrize(
        "overrides",
        [
            {"materialId": "1000000123"},  # not 80-series
            {"plant": "1600"},  # out of the two-plant scope
            {"materialId": "8099999999"},  # 80-series, but nothing knows it
            {"quantity": 1000000000},  # a typo that could never be corrected
            {"serialNumber": "X" * 65},  # wider than the column
            {"evidenceReference": "X" * 501},
        ],
    )
    def test_what_could_never_be_taken_back_is_refused(self, overrides) -> None:
        response = client.post(ATTESTATIONS, json=self.body(**overrides))
        assert response.status_code == 422
        assert client.get(ATTESTATIONS).json()["total"] == 0


@needs_views
class TestTheViewNoticesWritesFromElsewhere:
    """Explicit invalidation only covers writes through this process's router.

    `demo_seed` runs in another process, and justifications arrive through the
    WS7 endpoints -- so the view compares a fingerprint of its source tables on
    every read. Observed before this: after `demo_seed --clear` a running server
    kept reporting the seeded lines as covered until it was restarted.
    """

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

    def test_an_unchanged_table_serves_the_cached_view(self) -> None:
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.service import get_attestation_view, get_snapshot

        with get_sessionmaker()() as db:
            snapshot = get_snapshot(db)
            first = get_attestation_view(db, snapshot)
            assert get_attestation_view(db, snapshot) is first

    def test_an_attestation_written_outside_the_router_rebuilds_it(self) -> None:
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.service import get_attestation_view, get_snapshot

        with get_sessionmaker()() as db:
            snapshot = get_snapshot(db)
            first = get_attestation_view(db, snapshot)
            record(db, a_draft(), attestor="another.process")  # no reset call
            second = get_attestation_view(db, snapshot)
        assert second is not first
        assert second.source_fingerprint != first.source_fingerprint

    def test_a_new_justification_rebuilds_it(self) -> None:
        """Flushed, never committed -- the append-only table is left as found."""
        from app.assistant.models import Justification
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.service import (
            get_attestation_view,
            get_snapshot,
            reset_attestation_view,
        )

        with get_sessionmaker()() as db:
            snapshot = get_snapshot(db)
            first = get_attestation_view(db, snapshot)
            db.add(
                Justification(
                    kind="NEW_ACQUISITION",
                    reason_category="OTHER",
                    free_text="Fingerprint test.",
                    material_id="8000005632",
                    plant="1300",
                    author="test",
                )
            )
            db.flush()
            try:
                assert get_attestation_view(db, snapshot) is not first
            finally:
                db.rollback()
                reset_attestation_view()


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
        assert meta["total"] == 1181  # 1,225 less the 44 lines SAP deleted

    def test_with_no_attestations_everything_is_an_exception(self) -> None:
        """**This number is the business case.** 1,225 repair lines, zero
        attestations, because the control did not exist before this platform.

        If it ever drops without somebody having recorded an attestation, the
        check has been weakened rather than satisfied.
        """
        meta = client.get(f"{EXCEPTIONS}?pageSize=1").json()["meta"]
        assert meta["linesChecked"] == 1181
        assert meta["linesCovered"] == 0
        assert meta["byType"]["MISSING_ATTESTATION"] == 1181
        assert meta["total"] == sum(meta["byType"].values())

    def test_open_lines_are_warnings_and_closed_ones_are_information(self) -> None:
        missing = f"{EXCEPTIONS}?type=MISSING_ATTESTATION"
        assert client.get(f"{missing}&openOnly=true&pageSize=1").json()["total"] == 744
        meta = client.get(f"{EXCEPTIONS}?pageSize=1").json()["meta"]
        # 744 open repair lines, plus every UNJUSTIFIED_ACQUISITION -- a warning
        # whatever became of the repair, because the money was committed.
        assert meta["bySeverity"] == {
            "warning": 744 + meta["byType"]["UNJUSTIFIED_ACQUISITION"],
            "info": 437,
        }

    def test_unjustified_acquisitions_against_the_seeded_register(self) -> None:
        """2,331 new 80-series purchases; 345 were raised while a repair of the
        same part was open at the same plant, and none has a justification --
        the control did not exist when they were bought."""
        body = client.get(f"{EXCEPTIONS}?type=UNJUSTIFIED_ACQUISITION&pageSize=500").json()
        assert body["meta"]["acquisitionsChecked"] == 2331
        assert body["meta"]["byType"]["UNJUSTIFIED_ACQUISITION"] == 345
        assert "UNJUSTIFIED_ACQUISITION" in body["meta"]["typesRaised"]
        for item in body["items"]:
            assert item["acquisitionLine"]["documentNumber"]
            assert item["repairLine"]["documentNumber"] != item["acquisitionLine"]["documentNumber"]
        missing = client.get(f"{EXCEPTIONS}?type=MISSING_ATTESTATION&pageSize=1").json()
        assert missing["items"][0]["acquisitionLine"] is None

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
        assert statuses.get("Required", 0) < 1181
        assert sum(statuses.values()) == 1181

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


@needs_views
class TestTheRegisterCarriesTheRealDeclarationStatus:
    """The register's declarationStatus column, which W5.3 finally owns.

    Through W5.2 it was a hard-coded "Required" with a schema note saying W5.3
    owned it. The UI renders that column, so leaving it hard-coded after the
    attestation existed would have shown a screen full of "Required" that no
    attestation could ever change -- the exact quiet-wrongness I08 exists to
    stop.
    """

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

    def _statuses(self) -> dict[str, int]:
        rows: list[dict] = []
        page = 1
        while True:
            body = client.get(f"/api/i8/register?page={page}&pageSize=500").json()
            rows += body["items"]
            if len(rows) >= body["total"]:
                break
            page += 1
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["declarationStatus"]] = counts.get(row["declarationStatus"], 0) + 1
        return counts

    def test_with_no_attestations_every_line_reads_required(self) -> None:
        assert self._statuses() == {"Required": 1181}

    def test_seeding_moves_the_register_column_too(self) -> None:
        """Not just the declaration queue -- the register the UI actually renders."""
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.demo_seed import seed
        from app.initiatives.i8.service import reset_attestation_view

        with get_sessionmaker()() as db:
            seed(db, count=6)
        reset_attestation_view()

        counts = self._statuses()
        assert counts.get("Required", 0) < 1181
        assert counts.get("Completed", 0) >= 1
        assert sum(counts.values()) == 1181

    def test_the_repair_detail_agrees_with_the_register(self) -> None:
        """Two endpoints, one answer. A detail page that disagrees with the row
        it was opened from is worse than either being wrong on its own."""
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.demo_seed import seed
        from app.initiatives.i8.service import reset_attestation_view

        with get_sessionmaker()() as db:
            result = seed(db, count=3)
        reset_attestation_view()

        document, item = result.lines[0].rsplit("-", 1)
        row = client.get(f"/api/i8/register?pageSize=500").json()["items"]
        from_register = {r["id"]: r["declarationStatus"] for r in row}

        detail = client.get(f"/api/i8/register/{document}/{item}").json()
        line_id = detail["line"]["id"]
        if line_id in from_register:
            assert detail["line"]["declarationStatus"] == from_register[line_id]
        assert detail["line"]["declarationStatus"] in {
            "Required",
            "Pending",
            "Completed",
            "Flagged",
        }


@needs_views
class TestTheLifecycleTimelineAttestedStage:
    """Stage 2 of the lifecycle, which read "W5.3 owns this. Hook only."

    Both states are asserted, because they are different statements and the
    stage exists to keep them apart: "nobody assessed this part" is a finding,
    "we have not looked" is not.
    """

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

    def _attested_stage(self, document: str, item: str) -> dict:
        body = client.get(f"/api/i8/register/{document}/{item}").json()
        return next(s for s in body["timeline"] if s["stage"] == "attested")

    def test_with_no_attestation_it_says_what_was_looked_for(self) -> None:
        line = client.get("/api/i8/register?pageSize=1").json()["items"][0]
        document, item = line["id"].rsplit("-", 1)
        stage = self._attested_stage(document, item)

        assert stage["occurredAt"] is None
        # It must not read as a forgotten field. It should say there is no
        # assessment, and that this is expected rather than a data gap.
        assert "no recorded assessment" in stage["evidence"]
        assert "/api/i8/exceptions" in stage["evidence"]
        assert "Hook only" not in stage["evidence"]

    def test_with_an_attestation_it_carries_the_real_evidence(self) -> None:
        from app.core.db import get_sessionmaker
        from app.initiatives.i8.demo_seed import seed
        from app.initiatives.i8.service import reset_attestation_view

        with get_sessionmaker()() as db:
            result = seed(db, count=3)
        reset_attestation_view()

        document, item = result.lines[0].rsplit("-", 1)
        stage = self._attested_stage(document, item)

        assert stage["occurredAt"] is not None
        assert "Attestation ATT-" in stage["evidence"]
        assert "DEMO_SEED" in stage["evidence"]
        assert stage["daysSince"] is not None and stage["daysSince"] >= 0


class TestExplainCoverage:
    """What a POST actually achieved, in words a person can read.

    This exists because of a trap that is invisible until somebody uses the
    form. The timestamp is server-set (that is what makes it an audit record)
    and the extract is a frozen July-2026 snapshot, so an attestation recorded
    today is months outside the matching window of every line in the register
    and covers none of them.

    Both of those are correct. The danger is a UI that POSTs successfully, shows
    the row still reading "Required", and invites somebody to "fix" it by
    widening the window until it stops looking broken -- which would let an
    assessment from a different repair cycle count. So the API explains itself.
    """

    def _attestation(self, when: datetime, material="8000005632", plant="1300"):
        from app.initiatives.i8.models import RepairAttestation

        return RepairAttestation(
            id="ATT-EXPLAIN",
            material_id=material,
            plant=plant,
            quantity=Decimal(1),
            condition_description="Assessed.",
            fault_category="WEAR",
            recommendation="REPAIRABLE",
            attestor="tester",
            attested_at=when,
        )

    def test_it_reports_the_lines_it_covers(self) -> None:
        from app.initiatives.i8.attestation import explain_coverage

        raised = date(2026, 5, 1)
        lines = [
            FakeLine("4500", "10", "8000005632", "1300", raised),
            FakeLine("4500", "20", "8000005632", "1300", raised),
        ]
        covered, note = explain_coverage(
            self._attestation(datetime(2026, 5, 3, tzinfo=timezone.utc)), lines
        )

        # One attestation, two lines -- coverage is per material-plant. This is
        # open question 4, answered empirically.
        assert covered == ["4500-10", "4500-20"]
        assert "covers 2 repair lines" in note

    def test_no_repair_line_for_the_part_is_the_normal_case(self) -> None:
        """A part assessed BEFORE it is sent anywhere covers nothing yet, and
        that must not read as a failure."""
        from app.initiatives.i8.attestation import explain_coverage

        covered, note = explain_coverage(
            self._attestation(datetime(2026, 5, 3, tzinfo=timezone.utc)),
            [FakeLine("4500", "10", "8000009999", "1300", date(2026, 5, 1))],
        )
        assert covered == []
        assert "No repair line exists" in note
        assert "normal case" in note

    def test_the_trap_is_explained_rather_than_left_to_be_inferred(self) -> None:
        """The part HAS repair lines and they are all outside the window.

        The note must say so, give the gap, and state that the outstanding rows
        are the correct answer -- otherwise the next person widens the window.
        """
        from app.initiatives.i8.attestation import explain_coverage

        lines = [FakeLine("4500", "10", "8000005632", "1300", date(2025, 4, 7))]
        covered, note = explain_coverage(
            self._attestation(datetime(2026, 9, 15, tzinfo=timezone.utc)), lines
        )

        assert covered == []
        assert "covers none of the 1 repair line" in note
        assert "2025-04-07" in note
        assert "days from this assessment" in note
        assert "30-day matching window" in note
        assert "correct answer rather than a fault" in note

    def test_it_uses_the_same_window_as_the_queue(self) -> None:
        """An explanation derived from a second copy of the rule would
        eventually contradict the queue it is explaining."""
        from app.initiatives.i8.attestation import coverage as queue_coverage
        from app.initiatives.i8.attestation import explain_coverage

        # Exactly on the boundary: the queue and the explanation must agree.
        raised = date(2026, 5, 1)
        window = get_i8_settings().attestation_window_days
        attested = datetime.combine(
            raised + timedelta(days=window), datetime.min.time(), tzinfo=timezone.utc
        )
        line = FakeLine("4500", "10", "8000005632", "1300", raised)

        covered, _note = explain_coverage(self._attestation(attested), [line])
        assert covered == ["4500-10"]

        # One day further out, both must say no.
        covered, _note = explain_coverage(
            self._attestation(attested + timedelta(days=1)), [line]
        )
        assert covered == []


@needs_db
class TestThePostExplainsItself:
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

    def test_post_says_what_it_covered_and_get_does_not(self) -> None:
        created = client.post(
            ATTESTATIONS,
            json={
                "materialId": "8000005632",
                "plant": "1300",
                "quantity": 1,
                "conditionDescription": "Assessed.",
                "faultCategory": "WEAR",
                "recommendation": "REPAIRABLE",
            },
        )
        assert created.status_code == 201
        body = created.json()

        # On POST the caller is asking "did that do anything?", so it is told.
        assert body["coversRepairLines"] is not None
        assert body["coverageNote"]

        # On GET the caller is reading history, and the fields stay null rather
        # than recomputing a per-row answer nobody asked for.
        listed = client.get(ATTESTATIONS).json()["items"][0]
        assert listed["coversRepairLines"] is None
        assert listed["coverageNote"] is None
