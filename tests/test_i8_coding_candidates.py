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

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.core.ai import Completion, Message, TokenUsage
from app.initiatives.i8.coding_candidates import (
    ACTIONABLE,
    PROMPT_ID,
    CandidateLine,
    Verdict,
    fetch_repair_language_lines,
    find_twins,
    matched_keywords,
    parse_verdict,
    partition,
    screen,
)
from app.initiatives.i8.config import get_i8_settings
from app.main import app
from tests.i8_support import needs_views

client = TestClient(app)

CANDIDATES = "/api/i8/coding-candidates"


def a_line(**overrides) -> CandidateLine:
    base = dict(
        purchasing_document="4500000001",
        item="10",
        material_id="5000094118",
        plant="1300",
        short_text="RECONDITIONED BRAKE FOOTVALVE EMEM5893",
        matched=("recon",),
        raised_at=date(2026, 5, 1),
        item_category="0",
    )
    base.update(overrides)
    return CandidateLine(**base)


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

        # The QUERIES must carry no vocabulary -- the pattern reaches SQL as a
        # bind parameter built from config. Checked against the SQL constants
        # themselves rather than the whole file, which is full of these words in
        # prose and would make this test fail on a comment.
        from app.initiatives.i8.coding_candidates import _CANDIDATE_SQL, _TWIN_SQL

        assert ":pattern" in _CANDIDATE_SQL
        for sql in (_CANDIDATE_SQL, _TWIN_SQL):
            for keyword in cfg.repair_language_list:
                assert keyword not in sql.lower(), (
                    f"{keyword!r} is written into a query -- it belongs in config"
                )

    def test_the_pattern_escapes_each_keyword(self) -> None:
        """A keyword containing a regex character must narrow the search, not
        silently become a wildcard."""
        from app.initiatives.i8.config import I8Settings

        cfg = I8Settings(repair_language="a.b,c+d", _env_file=None)
        assert cfg.repair_language_pattern == r"a\.b|c\+d"


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
                a_line(item="10"),
                a_line(item="20"),
                a_line(item="30", material_id="5000094245"),
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
            [a_line(item="10"), a_line(item="20"), a_line(item="30")],
            in_universe=False,
        )
        assert len(llm.prompts) == 1


# --- Against the seeded extract --------------------------------------------


@needs_views
class TestAgainstTheRealExtract:
    @pytest.fixture
    def db(self):
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as session:
            yield session

    def test_the_keyword_screen_returns_what_the_plan_measured(self, db) -> None:
        """The plan's 305 and 6, plus the already-coded lines it counted
        separately. If this moves, find out what changed before anything else.
        """
        lines = fetch_repair_language_lines(db)
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
        groups = partition(fetch_repair_language_lines(db))
        assert len(groups.without_material) == 174
        assert sum(len(v) for v in groups.by_material.values()) == 122

    def test_122_lines_collapse_to_41_materials(self, db) -> None:
        groups = partition(fetch_repair_language_lines(db))
        assert len(groups.by_material) == 41

    def test_no_candidate_is_already_in_the_repairable_universe(self, db) -> None:
        """The cross-check the task plan asks for: a 404 from
        GET /api/i8/universe/{materialId} is what makes something a candidate.
        """
        from app.initiatives.i8.material_number import is_eighty_series

        groups = partition(fetch_repair_language_lines(db))
        assert not [m for m in groups.by_material if is_eighty_series(m)]

    def test_the_twin_corroboration_finds_the_part_coded_both_ways(self, db) -> None:
        """The strongest evidence this screen produces, and it needs no model.

        RECONDITIONED BRAKE FOOTVALVE EMEM5893 exists as 8000005737 (coded
        correctly) AND 5000094118 (not). Identical text, same physical part,
        both ways in one extract.
        """
        groups = partition(fetch_repair_language_lines(db))
        twins = find_twins(db, groups.by_material)

        assert "5000094118" in twins
        coded, shared = twins["5000094118"][0]
        assert coded == "8000005737"
        assert "BRAKE FOOTVALVE" in shared.upper()

    def test_the_screen_runs_end_to_end_with_no_model(self, db) -> None:
        """Every deterministic half works with no credentials at all."""
        candidates, stats = screen(db, use_model=False)

        assert stats.materials_screened == 41
        assert stats.lines_with_repair_language == 300
        assert stats.corroborated == 1
        # And it says plainly that nothing was judged.
        assert stats.by_verdict == {"UNSCREENED": 41}
        assert all(not c.is_actionable for c in candidates)

    def test_a_limit_is_reported_rather_than_applied_silently(self, db) -> None:
        """A silent cap reads as "we checked everything" when it did not."""
        _candidates, stats = screen(db, use_model=False, limit=5)
        assert stats.materials_screened == 5
        assert stats.materials_found == 41
        assert stats.was_truncated is True


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
        body = client.get(f"{CANDIDATES}?limit=3").json()
        assert body["meta"]["byVerdict"] == {"UNSCREENED": 3}

    def test_the_meta_reports_every_number_with_what_it_counts(self) -> None:
        meta = client.get(f"{CANDIDATES}?limit=1").json()["meta"]
        assert meta["linesWithText"] == 80880
        assert meta["linesWithRepairLanguage"] == 300
        assert meta["linesAlreadyEightySeries"] == 4
        assert meta["linesWithoutMaterial"] == 174
        assert meta["materialsFound"] == 41
        assert "repair" in meta["keywords"]

    def test_filtering_does_not_move_the_meta_counts(self) -> None:
        """"How much of the free text was searched" must not change because
        somebody asked for one verdict."""
        everything = client.get(CANDIDATES).json()
        filtered = client.get(f"{CANDIDATES}?corroboratedOnly=true").json()
        assert filtered["total"] < everything["total"]
        assert filtered["meta"]["materialsFound"] == everything["meta"]["materialsFound"]

    def test_every_item_carries_a_confidence_threshold_flag(self) -> None:
        """Default pass is unscreened -- empty confidence never meets a
        threshold, however low, so the flag reads False for every item."""
        for item in client.get(f"{CANDIDATES}?limit=5").json()["items"]:
            assert item["meetsConfidenceThreshold"] is False

    def test_confidence_threshold_filter_does_not_move_the_meta_counts(self) -> None:
        everything = client.get(f"{CANDIDATES}?limit=5").json()
        filtered = client.get(
            f"{CANDIDATES}?limit=5&meetsConfidenceThresholdOnly=true"
        ).json()
        assert everything["total"] == 5
        assert filtered["total"] == 0
        assert filtered["meta"]["materialsFound"] == everything["meta"]["materialsFound"]

    def test_the_corroborated_candidate_is_served_with_its_twin(self) -> None:
        body = client.get(f"{CANDIDATES}?corroboratedOnly=true").json()
        assert body["total"] == 1

        item = body["items"][0]
        assert item["materialId"] == "5000094118"
        assert item["isCorroborated"] is True
        assert item["twins"][0]["materialId"] == "8000005737"
        assert "BRAKE FOOTVALVE" in item["twins"][0]["sharedText"].upper()

    def test_every_item_carries_the_text_it_was_judged_on(self) -> None:
        for item in client.get(f"{CANDIDATES}?limit=5").json()["items"]:
            assert item["distinctTexts"]
            assert item["lines"]
            assert all(line["shortText"] for line in item["lines"])
            assert all(line["matchedKeywords"] for line in item["lines"])

    def test_no_item_claims_to_be_in_the_repairable_universe(self) -> None:
        """True here would mean the screen and W5.1 disagree about the same
        material, which is a bug worth failing over."""
        for item in client.get(CANDIDATES).json()["items"]:
            assert item["inRepairableUniverse"] is False
