"""The session identifier -- minting it, and reading one back that a human typed.

Why this is its own module, and why it is the first thing built
---------------------------------------------------------------
The session ID is the join between two systems that never talk to each other.
The assistant mints it; a person reads it off the screen and **types it into
SAP**; later something reads it back off the reservation and links the two. It
is an audit key that survives a round trip through human short-term memory and a
keyboard.

That round trip is the whole design constraint, and it produces three rules:

1. **Ten characters, maximum.** ``Bednr`` -- the field the ID is destined for --
   is ``Edm.String(10)``. ``Wempf``, the only exposed alternative, is 12, so 10
   is safe either way. The width is configuration, so pointing at a different
   field later is an ``.env`` change rather than a migration on audit history.

2. **No ambiguous characters.** Crockford base32 drops ``I``, ``L``, ``O`` and
   ``U``, so there is no glyph pair a person can confuse. Decoding also *repairs*
   the confusions anyway: a typed ``O`` is read as ``0`` and a typed ``I`` or
   ``L`` as ``1``, because the person did not make an error -- the font did.

3. **A mistyped ID must fail, not miss.** The last character is a checksum.
   Without one, a single wrong character produces a well-formed ID that matches
   no session, and "no session" is indistinguishable from "they never ran the
   assistant" -- which is exactly the compliance finding I13's
   ``MISSING_SESSION`` exists to raise. A typo would become a false
   non-compliance against a person who did everything right. The checksum turns
   that into ``INVALID_SESSION``, which is a different and honest answer.

What the placeholder data gets wrong
------------------------------------
``consumption_plans.csv`` carries 742 fabricated session IDs of the form
``SESS-000001``. That is **11 characters** and does not fit the field it is
standing in for, and a sequence leaks how many sessions exist. Nothing depends
on those IDs yet, which is why the format is settled now rather than after the
first real session is minted into an append-only table.

The deviation from Crockford's own check symbol, and exactly what it costs
---------------------------------------------------------------------------
Crockford's specification computes the check symbol modulo 37 -- a prime -- and
draws it from an alphabet extended with ``*~$=U``. Those five characters are
hostile here: they have to be typed into a SAP field by somebody reading off
another screen. So the check character is drawn from the same 32-character
alphabet as the rest of the ID, which keeps the whole identifier in one
character class.

32 is not prime, and that is the entire cost. It is worth being exact about
what survives rather than implying Crockford's guarantee:

* **Every single-character substitution is caught.** This is why the position
  weights are *odd* (1, 3, 5, ...) rather than the obvious 1, 2, 3: an odd
  number is invertible modulo 32, so a wrong character at any position always
  moves the sum. With even weights in the mix, a substitution that shifts a
  value by exactly 16 at an even-weighted position would cancel out silently.

* **Adjacent transpositions are caught unless the two characters' values differ
  by exactly 16** -- ``0``/``G``, ``1``/``H``, ``2``/``J`` and thirteen other
  pairs. Swapping two adjacent characters shifts the sum by twice their
  difference, and twice 16 is 32, which is zero. That is 16 of the 496 possible
  unordered pairs, so roughly 3% of adjacent transpositions pass. No
  weighted-sum scheme over a 32-symbol alphabet can close that gap: it needs a
  prime modulus, which needs symbols we cannot ask a person to type.

* The check character is included in the verification at its own odd weight, so
  a typo **in the check character itself** is caught too. Checking only the
  payload would leave the last character unprotected, and swapping it with its
  neighbour was undetectable until this was fixed.

**Why this is enough.** The checksum is a cheap local answer to "did you mistype
this?", not the only defence. The ID space is 32^8 -- about a trillion payloads
against a few thousand real sessions -- so anything that slips past the check
character still fails the lookup a moment later. What the checksum buys is the
ability to tell a requester "that was mistyped" instead of "no such session",
and those are different things to say to somebody who did everything right.
"""

from __future__ import annotations

import secrets

from app.core.config import Settings, get_settings

#: Crockford's base32 alphabet. No I, L, O or U -- see the module docstring.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_VALUE = {character: index for index, character in enumerate(ALPHABET)}

#: What a person typed -> what they meant. The glyphs Crockford excluded, mapped
#: back to the characters they are mistaken for.
_REPAIRS = str.maketrans({"I": "1", "L": "1", "O": "0", "i": "1", "l": "1", "o": "0"})

#: Separators somebody might add while transcribing (``S7K2-M4P8Q``).
_SEPARATORS = " -_\t\r\n"


class SessionIdError(ValueError):
    """A session ID is malformed, or its checksum does not match."""


#: The alphabet is the modulus. 32 symbols, 32 residues.
_MODULUS = len(ALPHABET)


def _weight(position: int) -> int:
    """The weight for one position: 1, 3, 5, 7, ...

    **Odd, and that is the load-bearing part.** An odd number is invertible
    modulo 32, so a wrong character at any position always moves the sum --
    which is what makes every single-character substitution detectable. The
    obvious 1, 2, 3, ... weighting silently misses a substitution that shifts a
    value by 16 at an even-weighted position.

    Distinct weights are what make transpositions detectable, since swapping two
    characters only changes the sum if their positions weigh differently.
    """
    return 2 * position + 1


def _check_character(body: str) -> str:
    """The checksum character that completes ``body``.

    Chosen so the weighted sum of the **whole** identifier -- check character
    included, at its own odd weight -- is zero modulo 32. Including it is what
    protects the last character: a scheme that only weighs the payload leaves
    the check character itself unverified, and swapping it with its neighbour
    then goes unnoticed.

    The prefix is included too. A wrong prefix is as much a typo as a wrong
    payload character, and there is no reason to leave it unchecked.
    """
    total = sum(_weight(index) * _VALUE[character] for index, character in enumerate(body))
    # The check character sits at the next position, so it carries that
    # position's weight. Odd weights are invertible mod 32, so there is exactly
    # one character that zeroes the sum -- no ambiguity, no unsolvable case.
    inverse = pow(_weight(len(body)), -1, _MODULUS)
    return ALPHABET[(-total * inverse) % _MODULUS]


def mint(settings: Settings | None = None) -> str:
    """A new session ID.

    Random rather than sequential, and that is not a preference. A sequence
    leaks how many sessions the platform has ever issued, and two environments
    running the same sequence mint colliding IDs for different sessions --
    which, on an append-only audit table, is unrecoverable. The same reasoning
    is already written into ``new_attestation_id`` in the I08 models.
    """
    settings = settings or get_settings()
    prefix = settings.assistant_session_id_prefix.strip().upper()
    payload_length = settings.assistant_session_id_length - len(prefix) - 1

    if payload_length < 1:
        raise SessionIdError(
            f"assistant_session_id_length ({settings.assistant_session_id_length}) "
            f"leaves no room for a payload after the prefix {prefix!r} and one "
            "check character"
        )

    body = prefix + "".join(secrets.choice(ALPHABET) for _ in range(payload_length))
    return body + _check_character(body)


def normalise(candidate: str | None) -> str:
    """Repair what a person typed, without judging whether it is valid.

    Upper-cases, drops the separators somebody might add while transcribing, and
    maps the excluded glyphs back to the characters they are mistaken for. This
    is deliberately forgiving: none of these are the requester's mistake, and an
    ID rejected over a lower-case letter would send somebody back to re-read a
    screen they read correctly.

    Split out from :func:`parse` so a caller that only needs the canonical form
    of a string -- a lookup key, a log line -- does not have to handle the
    exception :func:`parse` raises.
    """
    if candidate is None:
        return ""
    cleaned = candidate.strip().translate(_REPAIRS).upper()
    return "".join(character for character in cleaned if character not in _SEPARATORS)


def parse(candidate: str | None, settings: Settings | None = None) -> str:
    """The canonical ID, or raise :class:`SessionIdError` naming the rule it broke.

    Every failure says which rule failed rather than returning a bare "invalid".
    The caller turns this into I13's ``INVALID_SESSION``, and an exception-queue
    entry that cannot say *why* an ID was rejected is one nobody can action.
    """
    settings = settings or get_settings()
    session_id = normalise(candidate)

    if not session_id:
        raise SessionIdError("a session ID is required")

    expected_length = settings.assistant_session_id_length
    if len(session_id) != expected_length:
        raise SessionIdError(
            f"a session ID is {expected_length} characters; {session_id!r} is "
            f"{len(session_id)}"
        )

    prefix = settings.assistant_session_id_prefix.strip().upper()
    if not session_id.startswith(prefix):
        raise SessionIdError(
            f"a session ID starts with {prefix!r}: {session_id!r} does not"
        )

    unknown = sorted({character for character in session_id if character not in _VALUE})
    if unknown:
        # U is the interesting case: Crockford excludes it deliberately, so a
        # typed U is not a confusable to repair -- it is a character that was
        # never issued.
        raise SessionIdError(
            f"{session_id!r} contains {unknown}, which are not valid session-ID "
            f"characters (the alphabet is {ALPHABET})"
        )

    body, check = session_id[:-1], session_id[-1]
    if _check_character(body) != check:
        raise SessionIdError(
            f"{session_id!r} fails its checksum -- it was probably mistyped. "
            "This is a different answer from 'no such session': the ID as typed "
            "could never have been issued."
        )

    return session_id


def is_valid(candidate: str | None, settings: Settings | None = None) -> bool:
    """Whether :func:`parse` would accept this. For callers that want a boolean."""
    try:
        parse(candidate, settings)
    except SessionIdError:
        return False
    return True
