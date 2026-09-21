"""The session ID -- the one thing that has to be right before anything mints.

These are unit tests over a pure module: no database, no HTTP, no settings file.
The reason they are worth this much attention is that the ID is the only piece
of the assistant that passes through a human being. Everything else in the
platform moves between systems; this gets read off a screen, remembered for a
few seconds and typed into SAP, and it lands in an append-only table where a bad
format cannot be fixed afterwards.
"""

from __future__ import annotations

import pytest

from app.assistant import ids
from app.core.config import Settings

SETTINGS = Settings(assistant_session_id_length=10, assistant_session_id_prefix="S")


class TestShape:
    def test_it_fits_bednr(self) -> None:
        """Ten characters. ``Bednr`` is Edm.String(10) and that is the ceiling.

        The placeholder in consumption_plans.csv (``SESS-000001``) is eleven and
        would be truncated by the field it is standing in for.
        """
        assert len(ids.mint(SETTINGS)) == 10

    def test_it_starts_with_the_configured_prefix(self) -> None:
        assert ids.mint(SETTINGS).startswith("S")

    def test_width_is_configuration_not_a_constant(self) -> None:
        """Pointing at a different SAP field later must be an .env change.

        ``Wempf``, the only exposed alternative to Bednr, is twelve.
        """
        wider = Settings(assistant_session_id_length=12, assistant_session_id_prefix="S")
        minted = ids.mint(wider)
        assert len(minted) == 12
        assert ids.parse(minted, wider) == minted

    def test_a_width_with_no_room_for_a_payload_is_refused(self) -> None:
        """Rather than minting a prefix-plus-checksum with no random part."""
        with pytest.raises(ids.SessionIdError, match="leaves no room"):
            ids.mint(Settings(assistant_session_id_length=2, assistant_session_id_prefix="S"))

    def test_it_uses_no_ambiguous_characters(self) -> None:
        """No I, L, O or U anywhere, across enough draws to be meaningful."""
        for _ in range(500):
            assert not (set(ids.mint(SETTINGS)) & set("ILOU"))


class TestRoundTrip:
    def test_a_minted_id_parses(self) -> None:
        for _ in range(200):
            minted = ids.mint(SETTINGS)
            assert ids.parse(minted, SETTINGS) == minted

    def test_ids_are_not_sequential(self) -> None:
        """A sequence leaks how many sessions exist and collides across
        environments -- the same reasoning as ``new_attestation_id``."""
        minted = {ids.mint(SETTINGS) for _ in range(200)}
        assert len(minted) == 200


class TestTypingItBackIn:
    """The forgiving half. None of these are the requester's mistake."""

    def test_lower_case_is_accepted(self) -> None:
        minted = ids.mint(SETTINGS)
        assert ids.parse(minted.lower(), SETTINGS) == minted

    def test_surrounding_whitespace_is_accepted(self) -> None:
        minted = ids.mint(SETTINGS)
        assert ids.parse(f"  {minted}\n", SETTINGS) == minted

    def test_transcription_separators_are_accepted(self) -> None:
        """Somebody writing it down as ``S7K2-M4P8Q`` has not got it wrong."""
        minted = ids.mint(SETTINGS)
        hyphenated = f"{minted[:4]}-{minted[4:]}"
        assert ids.parse(hyphenated, SETTINGS) == minted

    @pytest.mark.parametrize("typed,meant", [("O", "0"), ("I", "1"), ("L", "1")])
    def test_the_excluded_glyphs_are_repaired_not_rejected(self, typed: str, meant: str) -> None:
        """A typed O is a 0. The person read it correctly; the font is at fault.

        Built as a genuinely valid ID (body plus its real check character), then
        typed back with the confusable glyph substituted in. It has to survive
        the checksum, which proves the repair happens *before* validation rather
        than instead of it.
        """
        body = "S" + meant + "23456KM"  # 9 characters; the check makes 10
        canonical = body + ids._check_character(body)
        assert len(canonical) == 10

        as_typed = "S" + typed + "23456KM" + canonical[-1]
        assert ids.parse(as_typed, SETTINGS) == canonical


class TestTheChecksum:
    """The strict half, and the reason it exists.

    Without a checksum a single mistyped character produces a well-formed ID
    that matches no session -- and "no session" is indistinguishable from "they
    never ran the assistant", which is the compliance finding I13's
    MISSING_SESSION raises. A typo would become a false non-compliance against
    somebody who did everything right.
    """

    def test_every_single_character_substitution_is_caught(self) -> None:
        minted = ids.mint(SETTINGS)
        for position in range(len(minted)):
            for replacement in ids.ALPHABET:
                if replacement == minted[position]:
                    continue
                corrupted = minted[:position] + replacement + minted[position + 1 :]
                assert not ids.is_valid(corrupted, SETTINGS), (
                    f"{corrupted} differs from {minted} in one character and was accepted"
                )

    def test_adjacent_transpositions_are_caught_except_the_documented_pairs(self) -> None:
        """The second most common typing error after substitution.

        Caught for every pair **except** two characters whose values differ by
        exactly 16 -- swapping shifts the sum by twice their difference, and
        twice 16 is the modulus. That hole is stated in the module docstring
        rather than glossed over, and it is asserted here in both directions so
        the limit cannot quietly widen: pairs that should be caught are caught,
        and the known-uncatchable pairs are the *only* ones that get through.

        Includes the last two positions, which is where the original scheme
        failed: it weighed only the payload, leaving the check character itself
        unprotected against a swap with its neighbour.
        """
        escaped: set[int] = set()

        for _ in range(200):
            minted = ids.mint(SETTINGS)
            for position in range(len(minted) - 1):
                left, right = minted[position], minted[position + 1]
                if left == right:
                    continue  # swapping identical characters is not a typo

                swapped = minted[:position] + right + left + minted[position + 2 :]
                gap = abs(ids.ALPHABET.index(left) - ids.ALPHABET.index(right))

                if ids.is_valid(swapped, SETTINGS):
                    assert gap == 16, (
                        f"{swapped} is {minted} with adjacent characters swapped "
                        f"and was accepted, but their values differ by {gap}, not 16 "
                        "-- the checksum is weaker than documented"
                    )
                    escaped.add(gap)

        # The documented hole is real, not a theoretical one we never hit --
        # a test that only ever saw the happy path would not be pinning anything.
        assert escaped == {16}, (
            "no 16-apart transposition was generated, so this test did not "
            "actually exercise the documented limit"
        )

    def test_a_typo_in_the_check_character_itself_is_caught(self) -> None:
        """The regression this scheme was rewritten for.

        Weighing only the payload left the last character unverified.
        """
        for _ in range(200):
            minted = ids.mint(SETTINGS)
            for replacement in ids.ALPHABET:
                if replacement == minted[-1]:
                    continue
                assert not ids.is_valid(minted[:-1] + replacement, SETTINGS)

    def test_a_failed_checksum_says_it_was_mistyped(self) -> None:
        """Not a bare 'invalid'. The caller turns this into INVALID_SESSION, and
        an exception-queue entry that cannot say why is one nobody can action."""
        minted = ids.mint(SETTINGS)
        wrong_check = ids.ALPHABET[(ids.ALPHABET.index(minted[-1]) + 1) % 32]
        with pytest.raises(ids.SessionIdError, match="checksum"):
            ids.parse(minted[:-1] + wrong_check, SETTINGS)


class TestRejection:
    def test_empty_is_rejected(self) -> None:
        with pytest.raises(ids.SessionIdError, match="required"):
            ids.parse("", SETTINGS)

    def test_none_is_rejected(self) -> None:
        with pytest.raises(ids.SessionIdError, match="required"):
            ids.parse(None, SETTINGS)

    def test_the_wrong_length_says_so(self) -> None:
        with pytest.raises(ids.SessionIdError, match="10 characters"):
            ids.parse("S123", SETTINGS)

    def test_the_placeholder_format_in_the_csv_does_not_fit(self) -> None:
        """``SESS-000001`` is what 742 fabricated plans carry today.

        Normalised it is ten characters (the hyphen is stripped) -- so length
        alone does NOT reject it, and without a checksum it would have been
        quietly accepted as a real session. This is the concrete case the check
        character earns its place on.
        """
        with pytest.raises(ids.SessionIdError):
            ids.parse("SESS-000001", SETTINGS)

    def test_a_missing_prefix_is_rejected(self) -> None:
        with pytest.raises(ids.SessionIdError, match="starts with"):
            ids.parse("X234567890", SETTINGS)

    def test_u_is_not_repaired_because_it_was_never_issued(self) -> None:
        """Crockford excludes U deliberately. Unlike O/I/L it is not a
        confusable to repair -- it is a character no minted ID can contain."""
        with pytest.raises(ids.SessionIdError, match="not valid session-ID"):
            ids.parse("SUUUUUUUUU", SETTINGS)


class TestNormalise:
    def test_it_does_not_validate(self) -> None:
        """Split from parse so a caller needing only a canonical lookup key does
        not have to handle an exception."""
        assert ids.normalise(" sess-000001 ") == "SESS000001"

    def test_none_becomes_empty_rather_than_raising(self) -> None:
        assert ids.normalise(None) == ""
