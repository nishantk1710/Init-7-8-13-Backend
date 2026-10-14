"""W5.5 -- finding repairable spares nobody coded as repairable.

Three tiers again: rules that need nothing, the screen against the seeded
extract, and the API.

The tests that earn their place are the ones about NOT reporting a clean
answer you did not compute. A screen with no credentials must say so; a
truncated run must say so; an unreadable model reply must say so. Every one of
those could quietly render as "no coding candidates found", which is the most
expensive wrong answer this module can give -- a false negative here is a
repairable spare bought new.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.ai import Completion, Message, TokenUsage
from app.initiatives.i8.coding_candidates import (
    ACTIONABLE,
    PROMPT_ID,
    CandidateLine,
    CandidateSource,
    CodingCandidate,
    Verdict,
    drop_converted_requisitions,
    drop_deleted_requisitions,
    fetch_repair_language_lines,
    find_twins,
    matched_keywords,
    parse_verdict,
    partition,
    rank_candidates,
    screen,
)
from app.initiatives.i8.coding_store import (
    StoredVerdict,
    forget_verdicts,
    save_verdict,
    text_fingerprint,
)
from app.initiatives.i8.config import get_i8_settings
from app.main import app
from tests.i8_support import needs_views

client = TestClient(app)

CANDIDATES = "/api/i8/coding-candidates"


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


def a_requisition(**overrides) -> CandidateLine:
    """A PR line. Same material and words, a different document."""
    base = dict(
        source=CandidateSource.PR.value,
        document="0010000123",
        document_item="0001",
        item_category="0",
        became=None,
    )
    base.update(overrides)
    return a_line(**base)


def a_movement(**overrides) -> CandidateLine:
    """A goods-movement line. No item category, by nature."""
    base = dict(
        source=CandidateSource.MOVEMENT.value,
        document="4900000001",
        document_item="0001",
        item_category=None,
    )
    base.update(overrides)
    return a_line(**base)


# --- The keyword pass, no database -----------------------------------------


class TestKeywordMatching:
    def test_it_matches_case_insensitively_and_on_stems(self) -> None:
        cfg = get_i8_settings()
        assert "repair" in matched_keywords("PUMP REPAIRS TO Y-CHUTE", cfg)
        assert "repair" in matched_keywords("plantrepairs oct", cfg)
        assert "refurb" in matched_keywords("REFURBISHED LINCOLN LN-25", cfg)

    def test_it_reports_every_keyword_a_line_hits(self) -> None:
        """Carried onto the row so "why was this picked up?" needs no
        explanation from anybody."""
        cfg = get_i8_settings()
        assert set(matched_keywords("REPAIR AND REBUILD GEARBOX", cfg)) == {
            "repair",
            "rebuild",
        }

    def test_an_unrelated_line_matches_nothing(self) -> None:
        assert matched_keywords("BOLT M12 X 40 GALVANISED", get_i8_settings()) == ()

    def test_the_vocabulary_is_configuration_not_code(self) -> None:
        """It is how VZI's buyers write, not a rule we get to fix."""
        cfg = get_i8_settings()
        assert "repair" in cfg.repair_language_list

        # The QUERIES must carry no vocabulary -- the keywords reach SQL as bind
        # parameters built from config. Checked against the SQL constants
        # themselves rather than the whole file, which is full of these words in
        # prose and would make this test fail on a comment.
        #
        # Every source, not just the PO one: a second free-text source is a
        # second chance to write a keyword into a query by hand.
        from app.initiatives.i8.coding_candidates import _SOURCES, _TWIN_TEXTS_SQL
        from app.shared.sql_lists import like_any

        rendered = [_TWIN_TEXTS_SQL]
        for spec in _SOURCES.values():
            assert "{keyword_match}" in spec.candidates, (
                f"{spec.source} has no keyword placeholder -- it would screen "
                f"every line of that source"
            )
            match, params = like_any(
                spec.text_column, "keyword", cfg.repair_language_like_patterns
            )
            assert set(params.values()) == set(cfg.repair_language_like_patterns)
            rendered.append(spec.candidates.replace("{keyword_match}", match))
            rendered.append(spec.text_total)

        for sql in rendered:
            for keyword in cfg.repair_language_list:
                assert keyword not in sql.lower(), (
                    f"{keyword!r} is written into a query -- it belongs in config"
                )

    def test_the_patterns_escape_each_keyword(self) -> None:
        """A keyword containing a LIKE wildcard must narrow the search, not
        silently become a wildcard -- ``[`` included, a class on SQL Server."""
        from app.initiatives.i8.config import I8Settings

        cfg = I8Settings(repair_language="50%,a_b,[x]", _env_file=None)
        assert cfg.repair_language_like_patterns == ("%50\\%%", "%A\\_B%", "%\\[X]%")


class TestPartition:
    """Where the population goes from 296 to 41, and why."""

    def test_a_line_with_no_material_is_never_a_candidate(self) -> None:
        """60% of the keyword hits have no material number at all.

        A coding candidate is a material that should have been coded 80-series.
        With no material there is nothing to re-code -- it is a free-text
        service purchase. Counted and reported, never screened.
        """
        groups = partition([a_line(material_id=None)])
        assert groups.by_material == {}
        assert len(groups.without_material) == 1

    def test_a_material_already_coded_repairable_is_not_a_candidate(self) -> None:
        """The convention worked. Measured at 6 lines."""
        groups = partition([a_line(material_id="8000005737")])
        assert groups.by_material == {}
        assert len(groups.already_coded) == 1

    def test_lines_are_grouped_by_material_not_left_per_line(self) -> None:
        """Coding is a property of the MATERIAL. Judging the same part four
        times could produce four verdicts and no way to choose between them."""
        groups = partition(
            [
                a_line(document_item="10"),
                a_line(document_item="20"),
                a_line(document_item="30", material_id="5000094245"),
            ]
        )
        assert set(groups.by_material) == {"5000094118", "5000094245"}
        assert len(groups.by_material["5000094118"]) == 2


# --- Reading the model's answer --------------------------------------------


class TestParseVerdict:
    def test_it_reads_a_clean_answer(self) -> None:
        parsed = parse_verdict(
            '{"verdict": "MISCODED_REPAIRABLE", "confidence": "high", "reason": "Says REFURBISHED."}'
        )
        assert parsed == (Verdict.MISCODED_REPAIRABLE, "high", "Says REFURBISHED.")

    def test_it_tolerates_prose_around_the_json(self) -> None:
        """Models add a sentence either side. Being strict about that would
        throw away a good answer over punctuation."""
        parsed = parse_verdict(
            'Sure!\n```json\n{"verdict": "REPAIR_SERVICE", "confidence": "low", "reason": "Callout."}\n```\nHope that helps.'
        )
        assert parsed is not None
        assert parsed[0] is Verdict.REPAIR_SERVICE

    def test_the_stub_provider_answer_is_unreadable_and_that_is_correct(self) -> None:
        """**The test that matters most here.**

        The stub returns fixed placeholder prose. If that ever parsed to a
        verdict, a run with no credentials would report real-looking findings.
        """
        assert (
            parse_verdict(
                "[stub completion a1b2c3d4e5f6] This text was generated without a "
                "model. Configure LLM_PROVIDER to use a real provider."
            )
            is None
        )

    @pytest.mark.parametrize(
        "response",
        [
            "",
            "no json here at all",
            '{"verdict": "PROBABLY_FINE"}',  # not one of ours
            '{"verdict": ["MISCODED_REPAIRABLE"]}',  # right word, wrong shape
            "{not valid json}",
            '["MISCODED_REPAIRABLE"]',  # a list, not an object
        ],
    )
    def test_anything_unreadable_returns_none_rather_than_a_default(
        self, response
    ) -> None:
        assert parse_verdict(response) is None

    def test_the_model_may_not_claim_unscreened(self) -> None:
        """UNSCREENED means "nobody judged this". It is ours to assign, and a
        model claiming it would erase the distinction the value exists for."""
        assert parse_verdict('{"verdict": "UNSCREENED", "confidence": "high"}') is None

    def test_an_unknown_confidence_degrades_to_low_rather_than_being_trusted(
        self,
    ) -> None:
        parsed = parse_verdict(
            '{"verdict": "UNCLEAR", "confidence": "absolutely certain", "reason": "x"}'
        )
        assert parsed is not None and parsed[1] == "low"

    def test_unclear_is_actionable_and_a_consumable_is_not(self) -> None:
        """UNCLEAR means a human should read the text -- that is work, not a
        dismissal."""
        assert Verdict.UNCLEAR in ACTIONABLE
        assert Verdict.MISCODED_REPAIRABLE in ACTIONABLE
        assert Verdict.CONSUMABLE_FOR_REPAIR not in ACTIONABLE
        assert Verdict.REPAIR_SERVICE not in ACTIONABLE


# --- The prompt ------------------------------------------------------------


class TestThePrompt:
    def test_it_is_on_disk_and_discoverable(self) -> None:
        from app.core.prompts import available_prompts, prompt_root

        assert PROMPT_ID in available_prompts()
        assert (prompt_root() / PROMPT_ID / "v1.md").is_file()

    def test_it_asks_for_exactly_the_fields_the_screen_supplies(self) -> None:
        """Prompt.render is strict in both directions, so a mismatch is a
        runtime failure on every material rather than a degraded answer."""
        from app.core.prompts import get_prompt

        assert get_prompt(PROMPT_ID).placeholders == frozenset(
            {"material", "plants", "line_count", "po_text"}
        )

    def test_it_routes_to_the_capable_model(self) -> None:
        """A false negative is a repairable spare bought new, so this is not a
        job for the cheap tier."""
        from app.core.model_registry import ROUTES

        assert ROUTES[PROMPT_ID].tier == "capable"

    def test_it_names_the_consumable_trap(self) -> None:
        """Roughly 57 of the screened lines are repair KITS and clamps.
        They are the most common wrong answer, so the prompt must call them
        out by name rather than hoping the model infers it."""
        from app.core.prompts import get_prompt

        template = get_prompt(PROMPT_ID).template.lower()
        assert "repair kit" in template
        assert "consumable_for_repair" in template


# --- A canned model, so the judgement path is testable ---------------------


class FakeLLM:
    """Returns a chosen answer. The stub cannot, by design.

    Patched over ``app.core.prompts.get_llm`` -- the consumer's namespace,
    which is the seam ``tests/test_ai.py`` already uses.
    """

    name = "fake"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, messages, **kwargs) -> Completion:
        self.prompts.append(messages[0].content)
        return Completion(
            text=self.response,
            model=kwargs.get("model") or "fake-model",
            provider="fake",
            usage=TokenUsage(1, 1),
        )

    def check_connection(self) -> None:
        return None


@pytest.fixture
def canned(monkeypatch):
    def install(response: str) -> FakeLLM:
        llm = FakeLLM(response)
        import app.core.prompts as prompt_module

        monkeypatch.setattr(prompt_module, "get_llm", lambda: llm)
        return llm

    return install


class TestScreeningOneMaterial:
    def _screen(self, llm_response, canned, **kwargs):
        from app.initiatives.i8.coding_candidates import screen_material

        llm = canned(llm_response)
        candidate = screen_material(
            "5000094118", [a_line()], in_universe=False, **kwargs
        )
        return candidate, llm

    def test_a_miscoded_line_is_flagged(self, canned) -> None:
        candidate, _ = self._screen(
            '{"verdict":"MISCODED_REPAIRABLE","confidence":"high","reason":"Says RECONDITIONED."}',
            canned,
        )
        assert candidate.verdict == "MISCODED_REPAIRABLE"
        assert candidate.is_actionable
        assert candidate.confidence == "high"

    def test_a_repair_service_line_is_not(self, canned) -> None:
        candidate, _ = self._screen(
            '{"verdict":"REPAIR_SERVICE","confidence":"high","reason":"Labour, not a part."}',
            canned,
        )
        assert candidate.verdict == "REPAIR_SERVICE"
        assert not candidate.is_actionable

    def test_every_candidate_carries_the_text_it_was_judged_on(self, canned) -> None:
        """A flag nobody can audit is a flag nobody will act on."""
        candidate, llm = self._screen(
            '{"verdict":"UNCLEAR","confidence":"low","reason":"Ambiguous."}', canned
        )
        assert candidate.texts == ("RECONDITIONED BRAKE FOOTVALVE EMEM5893",)
        # And the model really was shown that text.
        assert "RECONDITIONED BRAKE FOOTVALVE EMEM5893" in llm.prompts[0]

    def test_an_unreadable_answer_is_unscreened_and_keeps_the_raw_reply(
        self, canned
    ) -> None:
        candidate, _ = self._screen("I'm not sure, sorry!", canned)
        assert candidate.verdict == "UNSCREENED"
        assert candidate.raw_response == "I'm not sure, sorry!"
        assert not candidate.is_actionable

    def test_a_provider_failure_does_not_stop_the_run(self, monkeypatch) -> None:
        """One bad call must not lose the other 40 materials."""
        from app.core.ai import AIProviderError
        import app.core.prompts as prompt_module
        from app.initiatives.i8.coding_candidates import screen_material

        class Broken:
            name = "broken"

            def complete(self, *a, **k):
                raise AIProviderError("no credentials on this machine")

            def check_connection(self) -> None:
                raise AIProviderError("no")

        monkeypatch.setattr(prompt_module, "get_llm", lambda: Broken())
        candidate = screen_material("5000094118", [a_line()], in_universe=False)
        assert candidate.verdict == "UNSCREENED"
        assert "no credentials" in candidate.reason

    def test_the_prompt_is_sent_once_per_material_not_once_per_line(
        self, canned
    ) -> None:
        """41 model calls rather than 122, and strictly more evidence in each."""
        from app.initiatives.i8.coding_candidates import screen_material

        llm = canned('{"verdict":"UNCLEAR","confidence":"low","reason":"x"}')
        screen_material(
            "5000094118",
            [a_line(document_item="10"), a_line(document_item="20"), a_line(document_item="30")],
            in_universe=False,
        )
        assert len(llm.prompts) == 1


# --- The three free-text sources (FR-2) ------------------------------------
#
# FR-2 names three fields, and only one of them was ever read. These tests are
# about the two rules that decide what the other two contribute: a deleted
# requisition is not a request, and a requisition that became a PO is the same
# request twice.


class TestTheSourcesAreConfiguration:
    def test_the_default_reads_po_and_pr_but_not_movement(self) -> None:
        """MOVEMENT is off until somebody measures whether it finds anything.

        A 541 is a part already on its way to a vendor for repair, so its
        comments sit mostly on materials that ARE 80-series -- which makes them
        already_coded, not candidates. Shipping it on by default would spend
        model calls finding that out.
        """
        cfg = get_i8_settings()
        assert cfg.coding_candidate_source_list == ("PO", "PR")

    def test_every_named_source_has_a_query(self) -> None:
        from app.initiatives.i8.coding_candidates import _SOURCES

        assert set(_SOURCES) == {s.value for s in CandidateSource}

    def test_an_unknown_source_narrows_rather_than_crashing(self) -> None:
        """A typo in a deployment setting must not stop the app from starting.

        It narrows the screen visibly instead -- the response reports which
        sources ran -- and the opposite rule applies to the query parameter,
        where a typo is a caller's mistake and is told to them.
        """
        from app.initiatives.i8.config import I8Settings

        cfg = I8Settings(coding_candidate_sources="PO,NOPE", _env_file=None)
        assert cfg.coding_candidate_source_list == ("PO",)

    def test_configuring_nothing_still_reads_po(self) -> None:
        """A screen that reads nothing is never what anybody meant."""
        from app.initiatives.i8.config import I8Settings

        cfg = I8Settings(coding_candidate_sources="", _env_file=None)
        assert cfg.coding_candidate_source_list == ("PO",)


class TestDroppingRequisitionsThatAreNotEvidence:
    def test_a_deleted_requisition_is_not_a_request_for_anything(self) -> None:
        lines = [a_line(), a_requisition(deleted=True)]
        kept, dropped = drop_deleted_requisitions(lines)

        assert dropped == 1
        assert [line.source for line in kept] == [CandidateSource.PO.value]

    def test_a_live_requisition_survives(self) -> None:
        kept, dropped = drop_deleted_requisitions([a_requisition()])
        assert dropped == 0
        assert len(kept) == 1

    def test_a_requisition_that_became_a_po_in_this_run_is_dropped(self) -> None:
        """One request, counted once.

        The buyer kept the requester's wording, so the same words are in the
        run twice -- once as the PR, once as the PO it turned into. Counting
        both inflates every line figure and shows a reader two rows for one
        event.
        """
        po = a_line(document="4500000001", document_item="10")
        pr = a_requisition(became=("4500000001", "10"))

        kept, dropped = drop_converted_requisitions([po, pr])

        assert dropped == 1
        # The PO is what is kept: it is the document that exists in SAP now.
        assert [line.source for line in kept] == [CandidateSource.PO.value]

    def test_a_requisition_whose_po_is_not_in_the_run_survives(self) -> None:
        """The buyer rewrote the text, so the PO did not match the keywords.

        The requester's original wording is then the ONLY place the repair
        language survives, and dropping it would lose the finding entirely.
        """
        po = a_line(document="4500000001", document_item="10")
        pr = a_requisition(became=("4500000999", "20"))

        kept, dropped = drop_converted_requisitions([po, pr])

        assert dropped == 0
        assert len(kept) == 2

    def test_an_unconverted_requisition_survives(self) -> None:
        kept, dropped = drop_converted_requisitions([a_requisition(became=None)])
        assert dropped == 0
        assert len(kept) == 1

    def test_a_po_is_never_dropped_as_a_duplicate_of_itself(self) -> None:
        po = a_line(document="4500000001", document_item="10")
        kept, dropped = drop_converted_requisitions([po, po])
        assert dropped == 0
        assert len(kept) == 2


class TestLinesFromEverySourceGroupTogether:
    def test_one_material_gathers_its_po_pr_and_movement_lines(self) -> None:
        """The screen judges a MATERIAL, so every document carrying its text is
        evidence in the same call -- which is also why the source has to be on
        the line: three documents, three numbering schemes."""
        groups = partition([a_line(), a_requisition(), a_movement()])

        assert len(groups.by_material) == 1
        sources = {line.source for line in groups.by_material["5000094118"]}
        assert sources == {"PO", "PR", "MOVEMENT"}

    def test_a_movement_has_no_item_category(self) -> None:
        """An absence, not an unknown: a goods movement has no PSTYP at all."""
        assert a_movement().item_category is None


# --- Ranking (FR-2: "a ranked list") ---------------------------------------


def a_candidate(**overrides) -> CodingCandidate:
    base = dict(
        material_id="5000094118",
        lines=(a_line(),),
        verdict=Verdict.MISCODED_REPAIRABLE.value,
        confidence="high",
        reason="reads like a repairable spare",
        in_repairable_universe=False,
        twins=(),
    )
    base.update(overrides)
    return CodingCandidate(**base)


class TestRanking:
    def test_corroboration_outranks_any_verdict(self) -> None:
        """A twin is SAP contradicting itself -- the same description on the
        same part, coded both ways. That is evidence, and it beats the
        strongest opinion a model can offer."""
        opinion = a_candidate(
            material_id="1", verdict=Verdict.MISCODED_REPAIRABLE.value, confidence="high"
        )
        evidence = a_candidate(
            material_id="2",
            verdict=Verdict.UNCLEAR.value,
            confidence="low",
            twins=(("8000005737", "RECONDITIONED BRAKE FOOTVALVE"),),
        )

        assert [c.material_id for c in rank_candidates([opinion, evidence])] == ["2", "1"]

    def test_the_finding_comes_before_the_rejections(self) -> None:
        order = [
            Verdict.REPAIR_SERVICE.value,
            Verdict.UNSCREENED.value,
            Verdict.MISCODED_REPAIRABLE.value,
            Verdict.CONSUMABLE_FOR_REPAIR.value,
            Verdict.UNCLEAR.value,
        ]
        ranked = rank_candidates(
            [a_candidate(material_id=str(i), verdict=v) for i, v in enumerate(order)]
        )
        assert [c.verdict for c in ranked] == [
            Verdict.MISCODED_REPAIRABLE.value,
            Verdict.UNCLEAR.value,
            Verdict.CONSUMABLE_FOR_REPAIR.value,
            Verdict.REPAIR_SERVICE.value,
            Verdict.UNSCREENED.value,
        ]

    def test_unscreened_is_ranked_last_but_never_dropped(self) -> None:
        """Last because it is the ABSENCE of a judgement, not a weak one.

        Still served, because a screen that quietly hides what it did not judge
        is the one failure this whole module is built around.
        """
        ranked = rank_candidates(
            [
                a_candidate(material_id="1", verdict=Verdict.UNSCREENED.value, confidence=""),
                a_candidate(material_id="2", verdict=Verdict.REPAIR_SERVICE.value),
            ]
        )
        assert len(ranked) == 2
        assert ranked[-1].verdict == Verdict.UNSCREENED.value

    def test_confidence_breaks_a_verdict_tie(self) -> None:
        ranked = rank_candidates(
            [
                a_candidate(material_id="1", confidence="low"),
                a_candidate(material_id="2", confidence="high"),
                a_candidate(material_id="3", confidence="medium"),
            ]
        )
        assert [c.confidence for c in ranked] == ["high", "medium", "low"]

    def test_more_evidence_breaks_a_confidence_tie(self) -> None:
        ranked = rank_candidates(
            [
                a_candidate(material_id="1", lines=(a_line(),)),
                a_candidate(material_id="2", lines=(a_line(), a_line(), a_line())),
            ]
        )
        assert [c.material_id for c in ranked] == ["2", "1"]

    def test_the_order_is_stable_between_two_identical_runs(self) -> None:
        """So a reader can diff one run against the next."""
        candidates = [a_candidate(material_id=str(i)) for i in range(5)]
        assert [c.material_id for c in rank_candidates(candidates)] == [
            c.material_id for c in rank_candidates(list(reversed(candidates)))
        ]

    def test_an_unscreened_run_still_comes_back_in_a_useful_order(self) -> None:
        """Verdict and confidence say nothing when no model ran. Corroboration
        and evidence volume still do, and that is the point of ranking on
        them."""
        plain = a_candidate(material_id="1", verdict=Verdict.UNSCREENED.value, confidence="")
        twinned = a_candidate(
            material_id="2",
            verdict=Verdict.UNSCREENED.value,
            confidence="",
            twins=(("8000005737", "RECONDITIONED BRAKE FOOTVALVE"),),
        )
        assert [c.material_id for c in rank_candidates([plain, twinned])] == ["2", "1"]


# --- Not buying the same verdict twice -------------------------------------


class TestTheTextFingerprint:
    def test_the_same_texts_in_any_order_are_one_fingerprint(self) -> None:
        """Row order is not guaranteed across a reload, and a verdict must not
        be re-bought because the database returned two rows the other way
        round."""
        assert text_fingerprint(["A", "B"]) == text_fingerprint(["B", "A"])

    def test_whitespace_and_case_do_not_change_it(self) -> None:
        assert text_fingerprint(["Recon  Pump"]) == text_fingerprint(["RECON PUMP"])

    def test_changed_wording_changes_it(self) -> None:
        """The whole correctness rule. A re-worded PO line is new evidence, and
        serving the old verdict against it would miss exactly the change this
        screen exists to notice."""
        assert text_fingerprint(["RECON PUMP"]) != text_fingerprint(["RECON PUMP MK2"])

    def test_an_added_text_changes_it(self) -> None:
        assert text_fingerprint(["A"]) != text_fingerprint(["A", "B"])

    def test_empty_texts_are_ignored(self) -> None:
        assert text_fingerprint(["A", "", "   "]) == text_fingerprint(["A"])


class TestTheFastScreenServesStoredVerdicts:
    """The page-load path, with every database call faked out.

    The same rule is asserted against the real extract in
    :class:`TestAgainstTheRealExtract`, which skips wherever the July seed is
    absent -- that is, everywhere but a seeded environment. This covers it with
    no database at all, because the rule is about control flow rather than
    data: a verdict already on record must be served whether or not THIS
    request asked for a model.

    What went wrong without it (14-Oct-2026): the store was consulted only on
    the ``use_model=True`` branch, which nothing but the "Run AI screen" button
    takes. Every page load re-reported UNSCREENED for materials judged minutes
    earlier, and the screen read as though it had been thrown away on leaving
    the page. Nothing was lost -- the rows were in ``coding_verdicts`` the
    whole time and no code path read them.
    """

    @pytest.fixture
    def fake_screen(self, monkeypatch):
        """Run ``screen()`` with no connection: every DB call is replaced.

        Patched in the consumer's namespace, the same seam ``canned`` uses for
        the model. ``db`` is then never touched, so None is honest.
        """
        import contextlib

        import app.initiatives.i8.coding_candidates as mod

        written: list[dict] = []

        def install(*, lines, stored=(), **kwargs):
            @contextlib.contextmanager
            def fake_sources(*_a, **_k):
                yield mod.DIRECT_CODING

            by_material = {v.material_id: v for v in stored}
            monkeypatch.setattr(mod, "coding_sources", fake_sources)
            monkeypatch.setattr(
                mod, "fetch_repair_language_lines", lambda *a, **k: list(lines)
            )
            monkeypatch.setattr(mod, "find_twins", lambda *a, **k: {})
            monkeypatch.setattr(
                mod, "fetch_text_totals", lambda *a, **k: {"PO": len(lines)}
            )
            # The real one's contract, not a looser stand-in: a row whose
            # stored fingerprint no longer matches the text being judged is
            # NOT a hit. A fake that returned it regardless would let the
            # moved-text test below pass without the rule holding.
            monkeypatch.setattr(
                mod,
                "load_verdicts",
                lambda _db, fps: {
                    m: v
                    for m, v in by_material.items()
                    if fps.get(m) == v.text_fingerprint
                },
            )
            monkeypatch.setattr(mod, "save_verdict", lambda **kw: written.append(kw))

            candidates, stats = screen(
                None, sources=("PO",), staged=False, **kwargs
            )
            return candidates, stats, written

        return install

    def _stored(self, material: str, lines) -> StoredVerdict:
        return StoredVerdict(
            material_id=material,
            text_fingerprint=text_fingerprint(line.short_text for line in lines),
            verdict=Verdict.MISCODED_REPAIRABLE.value,
            confidence="high",
            reason="judged on a previous run",
            model="gpt-4o",
            provider="foundry",
            prompt_version=1,
            screened_at=datetime(2026, 10, 13, 15, 35, tzinfo=timezone.utc),
        )

    def test_a_stored_verdict_is_served_with_no_model_asked_for(
        self, fake_screen
    ) -> None:
        """The regression. This is what a page load does."""
        lines = [a_line()]
        candidates, stats, _written = fake_screen(
            lines=lines,
            stored=[self._stored("5000094118", lines)],
            use_model=False,
        )

        assert len(candidates) == 1
        assert candidates[0].verdict == Verdict.MISCODED_REPAIRABLE.value
        assert candidates[0].confidence == "high"
        assert candidates[0].provider == "foundry"
        assert stats.verdicts_reused == 1

    def test_the_fast_path_still_buys_nothing(self, fake_screen) -> None:
        """Serving a stored verdict must not have cost a model call -- the
        whole reason the fast and slow halves are separate."""
        lines = [a_line()]
        _candidates, stats, written = fake_screen(
            lines=lines,
            stored=[self._stored("5000094118", lines)],
            use_model=False,
        )

        assert written == []
        assert stats.verdicts_stored == 0

    def test_a_material_with_nothing_stored_is_still_unscreened(
        self, fake_screen
    ) -> None:
        """UNSCREENED has to keep meaning something after this change, or the
        screen starts claiming judgements it never made."""
        candidates, stats, _written = fake_screen(lines=[a_line()], use_model=False)

        assert candidates[0].verdict == Verdict.UNSCREENED.value
        assert stats.verdicts_reused == 0
        # And it says WHY, rather than leaving a reader to guess whether the
        # model was asked and failed.
        assert "no stored verdict" in candidates[0].reason

    def test_reuse_stored_false_ignores_the_store(self, fake_screen) -> None:
        """The escape hatch: the raw keyword pass with no history attached."""
        lines = [a_line()]
        candidates, stats, _written = fake_screen(
            lines=lines,
            stored=[self._stored("5000094118", lines)],
            use_model=False,
            reuse_stored=False,
        )

        assert candidates[0].verdict == Verdict.UNSCREENED.value
        assert stats.verdicts_reused == 0

    def test_a_stored_verdict_whose_text_moved_is_not_served(
        self, fake_screen
    ) -> None:
        """The correctness rule the fingerprint exists for, now that the fast
        path reads the store too: a re-worded PO line is new evidence, and the
        old verdict is about something else."""
        stored = self._stored("5000094118", [a_line(short_text="RECON PUMP")])
        candidates, _stats, _written = fake_screen(
            lines=[a_line(short_text="RECONDITIONED PUMP MK2")],
            stored=[stored],
            use_model=False,
        )

        assert candidates[0].verdict == Verdict.UNSCREENED.value


# --- Against the seeded extract --------------------------------------------


@needs_views
class TestAgainstTheRealExtract:
    """Every figure asserted here was measured against PO text alone.

    So every call pins ``sources=("PO",)`` rather than taking the configured
    default. Letting a deployment setting change what these assert would turn a
    documented measurement into a number nobody has ever checked -- and the
    first symptom would be this suite failing for a reason that is not a bug.
    """

    @pytest.fixture
    def db(self):
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as session:
            yield session

    def test_the_keyword_screen_returns_what_the_plan_measured(self, db) -> None:
        """The plan's 305 and 6, plus the already-coded lines it counted
        separately. If this moves, find out what changed before anything else.
        """
        lines = fetch_repair_language_lines(db, sources=("PO",))
        groups = partition(lines)

        # Pre-ruling: 311 lines, 6 already coded, 305 screened. The two-plant
        # scope of 21-Sep-2026 takes each down proportionally.
        assert len(lines) == 300
        assert len(groups.already_coded) == 4
        assert len(lines) - len(groups.already_coded) == 296

    def test_most_of_the_screened_lines_have_no_material_at_all(self, db) -> None:
        """The finding that reshaped this module. 174 of 296 -- 59% -- are
        free-text service purchases with nothing to re-code.

        Pre-ruling this was 183 of 305, also 60%. The proportion barely moved
        when the scope narrowed, which is worth knowing: the free-text repair
        spend is not a quirk of one site."""
        groups = partition(fetch_repair_language_lines(db, sources=("PO",)))
        assert len(groups.without_material) == 174
        assert sum(len(v) for v in groups.by_material.values()) == 122

    def test_122_lines_collapse_to_41_materials(self, db) -> None:
        groups = partition(fetch_repair_language_lines(db, sources=("PO",)))
        assert len(groups.by_material) == 41

    def test_no_candidate_is_already_in_the_repairable_universe(self, db) -> None:
        """The cross-check the task plan asks for: a 404 from
        GET /api/i8/universe/{materialId} is what makes something a candidate.
        """
        from app.initiatives.i8.material_number import is_eighty_series

        groups = partition(fetch_repair_language_lines(db, sources=("PO",)))
        assert not [m for m in groups.by_material if is_eighty_series(m)]

    def test_the_twin_corroboration_finds_the_part_coded_both_ways(self, db) -> None:
        """The strongest evidence this screen produces, and it needs no model.

        RECONDITIONED BRAKE FOOTVALVE EMEM5893 exists as 8000005737 (coded
        correctly) AND 5000094118 (not). Identical text, same physical part,
        both ways in one extract.
        """
        groups = partition(fetch_repair_language_lines(db, sources=("PO",)))
        twins = find_twins(db, groups.by_material)

        assert "5000094118" in twins
        coded, shared = twins["5000094118"][0]
        assert coded == "8000005737"
        assert "BRAKE FOOTVALVE" in shared.upper()

    def test_the_screen_runs_end_to_end_with_no_model(self, db) -> None:
        """Every deterministic half works with no credentials at all."""
        candidates, stats = screen(db, use_model=False, sources=("PO",))

        assert stats.materials_screened == 41
        assert stats.lines_with_repair_language == 300
        assert stats.corroborated == 1
        # And it says plainly that nothing was judged.
        assert stats.by_verdict == {"UNSCREENED": 41}
        assert all(not c.is_actionable for c in candidates)

    def test_a_limit_is_reported_rather_than_applied_silently(self, db) -> None:
        """A silent cap reads as "we checked everything" when it did not."""
        _candidates, stats = screen(db, use_model=False, limit=5, sources=("PO",))
        assert stats.materials_screened == 5
        assert stats.materials_found == 41
        assert stats.was_truncated is True

    def test_a_stored_verdict_comes_back_on_the_fast_screen(self, db) -> None:
        """The bug this guards (found 14-Oct-2026): leaving the page looked
        like it threw the AI screen away.

        Verdicts were written to ``coding_verdicts`` correctly and then read
        back only when ``use_model=True`` -- which nothing but the "Run AI
        screen" button sets. A page load ran the fast screen, never asked the
        store anything, and reported UNSCREENED for materials judged minutes
        earlier. The results were never lost; nobody was reading them.

        So this asserts the two halves that have to hold together: the stored
        verdict IS served on the fast path, and the fast path still calls no
        model to get it.
        """
        material = "5000094118"
        groups = partition(fetch_repair_language_lines(db, sources=("PO",)))
        fingerprint = text_fingerprint(
            line.short_text for line in groups.by_material[material]
        )

        save_verdict(
            db,
            material_id=material,
            fingerprint=fingerprint,
            verdict=Verdict.MISCODED_REPAIRABLE.value,
            confidence="high",
            reason="stored by this test",
            model="gpt-4o",
            provider="foundry",
            prompt_version=1,
            screened_at=datetime(2026, 10, 13, 15, 35, tzinfo=timezone.utc),
        )
        try:
            candidates, stats = screen(db, use_model=False, sources=("PO",))
            found = next(c for c in candidates if c.material_id == material)

            assert found.verdict == Verdict.MISCODED_REPAIRABLE.value
            assert found.confidence == "high"
            assert found.provider == "foundry"
            assert stats.verdicts_reused == 1
            # Nothing was bought to get it: the other 40 are still unjudged
            # and this run wrote nothing of its own.
            assert stats.by_verdict[Verdict.UNSCREENED.value] == 40
            assert stats.verdicts_stored == 0
        finally:
            # Left behind, this would fail the sibling test that asserts all
            # 41 come back UNSCREENED -- and only when the two happen to run
            # in that order, which is the worst kind of flake to chase.
            forget_verdicts(db, [material])

    def test_reuse_stored_false_still_shows_the_raw_keyword_pass(self, db) -> None:
        """The escape hatch, now that the fast screen consults the store.

        ``reuse_stored=False`` with no model is how to see what the keyword
        pass alone thinks, with no judgement history attached -- what you want
        when checking whether the deterministic half still finds a material at
        all.
        """
        material = "5000094118"
        groups = partition(fetch_repair_language_lines(db, sources=("PO",)))
        fingerprint = text_fingerprint(
            line.short_text for line in groups.by_material[material]
        )
        save_verdict(
            db,
            material_id=material,
            fingerprint=fingerprint,
            verdict=Verdict.MISCODED_REPAIRABLE.value,
            confidence="high",
            reason="stored by this test",
            model="gpt-4o",
            provider="foundry",
            prompt_version=1,
            screened_at=datetime(2026, 10, 13, 15, 35, tzinfo=timezone.utc),
        )
        try:
            _candidates, stats = screen(
                db, use_model=False, reuse_stored=False, sources=("PO",)
            )
            assert stats.by_verdict == {Verdict.UNSCREENED.value: 41}
            assert stats.verdicts_reused == 0
        finally:
            forget_verdicts(db, [material])


# --- The API ---------------------------------------------------------------


@needs_views
class TestTheApi:
    def test_the_route_is_mounted_and_read_only(self) -> None:
        spec = client.get("/openapi.json").json()
        assert "/api/i8/coding-candidates" in spec["paths"]
        assert set(spec["paths"]["/api/i8/coding-candidates"]) == {"get"}

    def test_it_defaults_to_the_fast_pass(self) -> None:
        """One model call per material is 246 seconds against live gpt-4o. A
        page load must not do that by accident."""
        body = client.get(f"{CANDIDATES}?limit=3&sources=PO").json()
        assert body["meta"]["byVerdict"] == {"UNSCREENED": 3}

    def test_the_meta_reports_every_number_with_what_it_counts(self) -> None:
        meta = client.get(f"{CANDIDATES}?limit=1&sources=PO").json()["meta"]
        assert meta["linesWithText"] == 80880
        assert meta["linesWithRepairLanguage"] == 300
        assert meta["linesAlreadyEightySeries"] == 4
        assert meta["linesWithoutMaterial"] == 174
        assert meta["materialsFound"] == 41
        assert "repair" in meta["keywords"]

    def test_filtering_does_not_move_the_meta_counts(self) -> None:
        """"How much of the free text was searched" must not change because
        somebody asked for one verdict."""
        everything = client.get(f"{CANDIDATES}?sources=PO").json()
        filtered = client.get(f"{CANDIDATES}?corroboratedOnly=true&sources=PO").json()
        assert filtered["total"] < everything["total"]
        assert filtered["meta"]["materialsFound"] == everything["meta"]["materialsFound"]

    def test_every_item_carries_a_confidence_threshold_flag(self) -> None:
        """Default pass is unscreened -- empty confidence never meets a
        threshold, however low, so the flag reads False for every item."""
        for item in client.get(f"{CANDIDATES}?limit=5&sources=PO").json()["items"]:
            assert item["meetsConfidenceThreshold"] is False

    def test_confidence_threshold_filter_does_not_move_the_meta_counts(self) -> None:
        everything = client.get(f"{CANDIDATES}?limit=5&sources=PO").json()
        filtered = client.get(
            f"{CANDIDATES}?limit=5&sources=PO&meetsConfidenceThresholdOnly=true"
        ).json()
        assert everything["total"] == 5
        assert filtered["total"] == 0
        assert filtered["meta"]["materialsFound"] == everything["meta"]["materialsFound"]

    def test_the_corroborated_candidate_is_served_with_its_twin(self) -> None:
        body = client.get(f"{CANDIDATES}?corroboratedOnly=true&sources=PO").json()
        assert body["total"] == 1

        item = body["items"][0]
        assert item["materialId"] == "5000094118"
        assert item["isCorroborated"] is True
        assert item["twins"][0]["materialId"] == "8000005737"
        assert "BRAKE FOOTVALVE" in item["twins"][0]["sharedText"].upper()

    def test_every_item_carries_the_text_it_was_judged_on(self) -> None:
        for item in client.get(f"{CANDIDATES}?limit=5&sources=PO").json()["items"]:
            assert item["distinctTexts"]
            assert item["lines"]
            assert all(line["shortText"] for line in item["lines"])
            assert all(line["matchedKeywords"] for line in item["lines"])

    def test_no_item_claims_to_be_in_the_repairable_universe(self) -> None:
        """True here would mean the screen and W5.1 disagree about the same
        material, which is a bug worth failing over."""
        for item in client.get(f"{CANDIDATES}?sources=PO").json()["items"]:
            assert item["inRepairableUniverse"] is False


# --- The cache in front of the store ---------------------------------------


class TestTheScreenCacheDropsStaleFastScreens:
    """The second half of the same bug, and the half that would have hidden
    the first fix.

    ``get_coding_screen`` memoises per (use_model, limit, sources). The
    page-load entry is built once per worker and was only ever cleared on a
    snapshot rebuild -- so even once the fast screen learned to read stored
    verdicts, it would have gone on serving a result computed before those
    verdicts existed, until the snapshot happened to refresh or the app
    restarted.

    No database here: what is being asserted is when the cache is dropped, and
    that is decided entirely by ``stats.verdicts_stored``.
    """

    @pytest.fixture(autouse=True)
    def clean_cache(self):
        from app.initiatives.i8.service import reset_coding_screen

        reset_coding_screen()
        yield
        reset_coding_screen()

    @pytest.fixture
    def builds(self, monkeypatch):
        """Record every build, and let each one say how much it stored."""
        import app.initiatives.i8.service as svc
        from app.initiatives.i8.coding_candidates import ScreenStats

        calls: list[tuple[bool, int | None]] = []

        def install(stored_when_model: int):
            def fake_build(db, snapshot, cfg, *, use_model, limit, sources):
                calls.append((use_model, limit))
                return svc.CodingScreen(
                    candidates=(),
                    stats=ScreenStats(
                        verdicts_stored=stored_when_model if use_model else 0
                    ),
                    built_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
                    build_seconds=0.0,
                    used_model=use_model,
                )

            monkeypatch.setattr(svc, "build_coding_screen", fake_build)
            return calls

        return install

    def test_a_run_that_stores_verdicts_invalidates_the_page_load_screen(
        self, builds
    ) -> None:
        """The sequence the user hit: screen, navigate away, come back."""
        import app.initiatives.i8.service as svc

        calls = builds(stored_when_model=3)
        cfg = get_i8_settings()

        # Two page loads -- the second is served from cache, as it should be.
        svc.get_coding_screen(None, None, cfg, use_model=False)
        svc.get_coding_screen(None, None, cfg, use_model=False)
        assert calls == [(False, None)]

        # "Run AI screen" judges and stores.
        svc.get_coding_screen(None, None, cfg, use_model=True, limit=10)

        # Coming back to the page must now rebuild rather than serve the
        # screen that was computed before those verdicts existed.
        svc.get_coding_screen(None, None, cfg, use_model=False)
        assert calls == [(False, None), (True, 10), (False, None)]

    def test_a_run_that_stores_nothing_leaves_the_cache_alone(self, builds) -> None:
        """Every batch after the first mostly REUSES verdicts. Dropping the
        cache on those too would rebuild the fast screen a dozen times during
        one run, for no change in what it would say."""
        import app.initiatives.i8.service as svc

        calls = builds(stored_when_model=0)
        cfg = get_i8_settings()

        svc.get_coding_screen(None, None, cfg, use_model=False)
        svc.get_coding_screen(None, None, cfg, use_model=True, limit=10)
        svc.get_coding_screen(None, None, cfg, use_model=False)

        assert calls == [(False, None), (True, 10)]

    def test_the_model_runs_own_entry_survives_its_own_invalidation(
        self, builds
    ) -> None:
        """Only the fast screens are stale. A model run that stored something
        is the newest thing in the cache, and re-running it costs minutes."""
        import app.initiatives.i8.service as svc

        calls = builds(stored_when_model=3)
        cfg = get_i8_settings()

        svc.get_coding_screen(None, None, cfg, use_model=True, limit=10)
        svc.get_coding_screen(None, None, cfg, use_model=True, limit=10)

        assert calls == [(True, 10)]
