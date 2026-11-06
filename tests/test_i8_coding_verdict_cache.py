"""Repair Candidates: a cached screen notices verdicts another worker stored.

The screen is cached per PROCESS; the verdicts behind it are shared. Prod runs
more than one worker, so the run that judged three materials on 6-Nov-2026
cleared only its own cached screen -- and the other worker went on serving a
list built before those verdicts existed. A refresh then showed 113 screened or
110, depending on which worker answered, and the three looked like they had
been thrown away. They had not: nobody was reading them.

What closes that is a stamp of the verdict table carried on the cached screen,
checked on every cache hit (``coding_store.verdict_stamp``) and, when it has
moved, re-read onto the candidates already built
(``coding_candidates.reapply_stored_verdicts``) rather than paying the 10-24 s
rebuild for facts that cannot have changed.

No database: the two things being asserted are WHEN the cache is re-read and
WHAT the re-read produces, and both are decided above the session.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import app.initiatives.i8.service as svc
from app.initiatives.i8 import coding_candidates as cc
from app.initiatives.i8.coding_candidates import (
    CandidateLine,
    CandidateSource,
    CodingCandidate,
    ScreenStats,
    Verdict,
    reapply_stored_verdicts,
)
from app.initiatives.i8.coding_store import StoredVerdict, verdict_stamp
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.new_item_price import NewItemPrice

JUDGED = datetime(2026, 11, 6, 10, 31, tzinfo=timezone.utc)


def a_line(**overrides) -> CandidateLine:
    base = dict(
        source=CandidateSource.PO.value,
        document="4500000001",
        document_item="10",
        material_id="5000094118",
        plant="1300",
        short_text="RECONDITIONED BRAKE FOOTVALVE EMEM5893",
        matched=("recon",),
        raised_at=date(2026, 5, 1),
        item_category="0",
    )
    base.update(overrides)
    return CandidateLine(**base)


def a_candidate(material_id: str = "5000094118", **overrides) -> CodingCandidate:
    base = dict(
        material_id=material_id,
        lines=(a_line(material_id=material_id),),
        verdict=Verdict.UNSCREENED.value,
        confidence="",
        reason="the model pass was not requested",
        in_repairable_universe=False,
        twins=(),
        new_item_prices=(NewItemPrice(plant="1300", price=Decimal("1250.00")),),
    )
    base.update(overrides)
    return CodingCandidate(**base)


def a_stored(material: str, candidate: CodingCandidate, **overrides) -> StoredVerdict:
    base = dict(
        material_id=material,
        text_fingerprint=cc.text_fingerprint(
            line.short_text for line in candidate.lines
        ),
        verdict=Verdict.MISCODED_REPAIRABLE.value,
        confidence="high",
        reason="the text describes a reconditioned part",
        model="gpt-4o",
        provider="foundry",
        prompt_version=3,
        screened_at=JUDGED,
    )
    base.update(overrides)
    return StoredVerdict(**base)


class TestReApplyingTheStoredVerdicts:
    """The cheap half: the same candidates, judged as the table has them now."""

    def test_a_verdict_stored_elsewhere_lands_on_the_cached_candidate(
        self, monkeypatch
    ) -> None:
        candidate = a_candidate()
        stored = a_stored("5000094118", candidate)
        monkeypatch.setattr(cc, "load_verdicts", lambda db, fps: {"5000094118": stored})

        (judged,), stats = reapply_stored_verdicts(None, [candidate], ScreenStats())

        assert judged.verdict == Verdict.MISCODED_REPAIRABLE.value
        assert judged.confidence == "high"
        assert judged.reason == stored.reason
        assert (judged.model, judged.provider) == ("gpt-4o", "foundry")
        assert judged.screened_at == JUDGED
        assert stats.verdicts_reused == 1
        assert stats.by_verdict == {Verdict.MISCODED_REPAIRABLE.value: 1}

    def test_the_facts_about_the_material_are_carried_over_untouched(
        self, monkeypatch
    ) -> None:
        """Lines, twins and the price column are about the extract, not the
        model. Rebuilding the candidate rather than replacing its verdict used
        to drop the prices -- the column would have gone blank on a re-read."""
        candidate = a_candidate(twins=(("8000005737", "RECONDITIONED BRAKE"),))
        monkeypatch.setattr(
            cc,
            "load_verdicts",
            lambda db, fps: {"5000094118": a_stored("5000094118", candidate)},
        )

        (judged,), _ = reapply_stored_verdicts(None, [candidate], ScreenStats())

        assert judged.lines == candidate.lines
        assert judged.twins == candidate.twins
        assert judged.new_item_prices == candidate.new_item_prices
        assert judged.in_repairable_universe is False

    def test_a_material_with_nothing_on_record_keeps_its_reason(
        self, monkeypatch
    ) -> None:
        """Unscreened is not a verdict to overwrite with a different unscreened:
        the reason is what tells a reader whether the model failed or was never
        asked."""
        candidate = a_candidate(reason="model unavailable: no credentials")
        monkeypatch.setattr(cc, "load_verdicts", lambda db, fps: {})

        (same,), stats = reapply_stored_verdicts(None, [candidate], ScreenStats())

        assert same is candidate
        assert stats.verdicts_reused == 0

    def test_a_verdict_that_has_been_forgotten_goes_back_to_unscreened(
        self, monkeypatch
    ) -> None:
        """forget_verdicts is a deliberate re-screen. Keeping the answer it
        just deleted would make the clearing invisible."""
        candidate = a_candidate(
            verdict=Verdict.MISCODED_REPAIRABLE.value,
            confidence="high",
            reason="judged earlier",
            model="gpt-4o",
            provider="foundry",
            screened_at=JUDGED,
        )
        monkeypatch.setattr(cc, "load_verdicts", lambda db, fps: {})

        (back,), stats = reapply_stored_verdicts(None, [candidate], ScreenStats())

        assert back.verdict == Verdict.UNSCREENED.value
        assert back.confidence == ""
        assert back.reason == cc.FORGOTTEN_VERDICT_REASON
        assert (back.model, back.provider, back.screened_at) == ("", "", None)
        assert stats.verdicts_reused == 0

    def test_the_newly_judged_rows_are_ranked_back_to_the_top(
        self, monkeypatch
    ) -> None:
        """UNSCREENED sorts last. A material judged since the screen was built
        belongs where its verdict puts it, not where it sat unjudged."""
        quiet = a_candidate(material_id="5000053306")
        loud = a_candidate(material_id="5000087263")
        monkeypatch.setattr(
            cc,
            "load_verdicts",
            lambda db, fps: {"5000087263": a_stored("5000087263", loud)},
        )

        ranked, stats = reapply_stored_verdicts(None, [quiet, loud], ScreenStats())

        assert [c.material_id for c in ranked] == ["5000087263", "5000053306"]
        assert stats.by_verdict == {
            Verdict.MISCODED_REPAIRABLE.value: 1,
            Verdict.UNSCREENED.value: 1,
        }

    def test_the_re_read_never_claims_to_have_stored_anything(
        self, monkeypatch
    ) -> None:
        """verdicts_stored is what tells the caller to drop its cached screens.
        A pass that only read must not set it, or every worker would invalidate
        every other worker in turn."""
        candidate = a_candidate()
        monkeypatch.setattr(
            cc,
            "load_verdicts",
            lambda db, fps: {"5000094118": a_stored("5000094118", candidate)},
        )

        _, stats = reapply_stored_verdicts(
            None, [candidate], ScreenStats(verdicts_stored=3)
        )

        assert stats.verdicts_stored == 0


class TestTheStampDecidesWhetherToReRead:
    """``_refreshed``: the check on every cache hit."""

    @pytest.fixture(autouse=True)
    def clean_cache(self):
        svc.reset_coding_screen()
        yield
        svc.reset_coding_screen()

    def _cached(self, stamp) -> tuple[tuple, svc.CodingScreen]:
        key = (False, None, get_i8_settings().coding_candidate_source_list)
        screen = svc.CodingScreen(
            candidates=(a_candidate(),),
            stats=ScreenStats(),
            built_at=datetime(2026, 11, 6, 8, 51, tzinfo=timezone.utc),
            build_seconds=22.6,
            used_model=False,
            verdict_stamp=stamp,
        )
        svc._screens[key] = screen
        return key, screen

    def test_an_unchanged_table_serves_the_cache_untouched(self, monkeypatch) -> None:
        key, cached = self._cached((110, JUDGED))
        monkeypatch.setattr(svc, "read_verdict_stamp", lambda db: (110, JUDGED))
        monkeypatch.setattr(
            svc, "reapply_stored_verdicts", lambda *a: pytest.fail("re-read anyway")
        )

        assert svc._refreshed(object(), key, cached) is cached

    def test_a_table_that_moved_is_re_read_and_the_cache_replaced(
        self, monkeypatch
    ) -> None:
        """The prod sequence: this worker's screen was built at 08:51, the
        other worker stored three verdicts at 10:31."""
        key, cached = self._cached((110, datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)))
        judged = (a_candidate(verdict=Verdict.MISCODED_REPAIRABLE.value),)
        monkeypatch.setattr(svc, "read_verdict_stamp", lambda db: (113, JUDGED))
        monkeypatch.setattr(
            svc, "reapply_stored_verdicts", lambda db, c, s: (list(judged), s)
        )

        refreshed = svc._refreshed(object(), key, cached)

        assert refreshed.candidates == judged
        assert refreshed.verdict_stamp == (113, JUDGED)
        # Kept, so the next request does not re-read it all over again.
        assert svc._screens[key] is refreshed
        assert refreshed.built_at == cached.built_at  # still that build's screen

    def test_a_stamp_that_cannot_be_read_serves_the_cache(self, monkeypatch) -> None:
        """A paused or resuming database must cost the check, not the page."""
        key, cached = self._cached((110, JUDGED))
        monkeypatch.setattr(svc, "read_verdict_stamp", lambda db: None)
        monkeypatch.setattr(
            svc, "reapply_stored_verdicts", lambda *a: pytest.fail("re-read anyway")
        )

        assert svc._refreshed(object(), key, cached) is cached

    def test_without_a_session_the_cache_is_served_as_it_stands(self) -> None:
        key, cached = self._cached((110, JUDGED))

        assert svc._refreshed(None, key, cached) is cached

    def test_the_badge_is_checked_against_the_same_table(self, monkeypatch) -> None:
        """Otherwise the menu goes on offering rows the page has already
        judged -- the two read the same per-worker cache."""
        key, _ = self._cached((110, datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)))
        judged = [a_candidate(verdict=Verdict.MISCODED_REPAIRABLE.value)]
        monkeypatch.setattr(svc, "read_verdict_stamp", lambda db: (113, JUDGED))
        monkeypatch.setattr(
            svc, "reapply_stored_verdicts", lambda db, c, s: (judged, s)
        )

        assert svc.peek_unscreened_count(get_i8_settings(), db=object()) == 0
        assert svc._screens[key].candidates == tuple(judged)

    def test_the_badge_without_a_session_still_answers(self) -> None:
        self._cached((110, JUDGED))

        assert svc.peek_unscreened_count(get_i8_settings()) == 1


class TestTheStampItself:
    def test_an_unreadable_table_is_none_rather_than_a_raised_error(self) -> None:
        from sqlalchemy.exc import OperationalError

        class Failing:
            rolled_back = False

            def execute(self, _statement):
                raise OperationalError("select", {}, Exception("08001"))

            def rollback(self):
                self.rolled_back = True

        db = Failing()
        assert verdict_stamp(db) is None
        assert db.rolled_back, "a failed read must not leave the session dirty"

    def test_it_reports_the_row_count_and_the_newest_screening(self) -> None:
        class Counting:
            def execute(self, _statement):
                class Row:
                    def one(self):
                        return (113, JUDGED)

                return Row()

        assert verdict_stamp(Counting()) == (113, JUDGED)
