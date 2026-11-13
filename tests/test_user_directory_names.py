"""Which signed-in person a typed name stands for -- ``user_directory.oid_for_name``.

The people are made up. Their names have middle names, as the real directory's do, which is what makes matching a
typed first-and-last name to the full name the point of the word match.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.user_directory import oid_for_name
from app.models.app_user import AppUser
from app.models.base import Base

PEOPLE = {
    "asha": ("00000000-0000-0000-0000-0000000000a1", "Asha Meera Rao", "asha.rao@example.com"),
    "ben": ("00000000-0000-0000-0000-0000000000b2", "Ben Alan Carter", "ben.carter@example.com"),
    "chen": ("00000000-0000-0000-0000-0000000000c3", "Chen Wei Lin", "CLin@example.com"),
    "dara": ("00000000-0000-0000-0000-0000000000d4", "Dara Neil Moyo", "dara.moyo@example.com"),
    "eli": ("00000000-0000-0000-0000-0000000000e5", "Eli Marcus Stone", "eli.stone@example.com"),
}
SECOND_ASHA = "00000000-0000-0000-0000-0000000000f6"
SECOND_BEN = "00000000-0000-0000-0000-0000000000f7"


def oid(who: str) -> str:
    return PEOPLE[who][0]


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[AppUser.__table__])
    with Session(engine) as session:
        for person_oid, name, email in PEOPLE.values():
            session.add(AppUser(oid=person_oid, name=name, email=email))
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.parametrize(
    "typed, who",
    [
        ("Asha Meera Rao", "asha"),  # the whole name
        ("asha.rao@example.com", "asha"),  # the e-mail address
        ("ASHA.RAO@EXAMPLE.COM", "asha"),
        ("Asha Rao", "asha"),  # first and last, no middle name
        ("Rao Asha", "asha"),  # any order
        ("  asha    RAO ", "asha"),  # case and spaces
        ("Rao, Asha", "asha"),  # punctuation
        ("Asha Meera", "asha"),  # any two of its words
        ("Ben Carter", "ben"),
        ("Chen Lin", "chen"),
        ("clin@example.com", "chen"),
    ],
)
def test_these_find_the_person(db, typed: str, who: str) -> None:
    assert oid_for_name(db, typed) == oid(who)


@pytest.mark.parametrize(
    "typed",
    [
        "Asha",  # one word: too easy to mean someone else
        "Rao",
        "Carter",
        "Ash Rao",  # not a whole word
        "Asha Stone",  # a word her name does not have
        "Ahsa Rao",  # a typo
        "Asha Asha",  # one word said twice
        "A Stranger",  # nobody signed in with it
        "someone@example.com",  # an e-mail nobody has
        "asha@example.com",  # an e-mail is exact, never a word match
        "",
        "   ",
        None,
    ],
)
def test_these_find_nobody(db, typed) -> None:
    assert oid_for_name(db, typed) is None


def test_two_people_matching_the_words_means_nobody_is_guessed(db) -> None:
    db.add(AppUser(oid=SECOND_ASHA, name="Asha Kiran Rao", email="a.rao2@example.com"))
    db.commit()

    assert oid_for_name(db, "Asha Rao") is None  # two people have both words
    # ... but the whole name, the e-mail address or a word only one of them has still finds the one meant.
    assert oid_for_name(db, "Asha Meera Rao") == oid("asha")
    assert oid_for_name(db, "a.rao2@example.com") == SECOND_ASHA
    assert oid_for_name(db, "Asha Kiran") == SECOND_ASHA


def test_two_people_with_exactly_the_same_whole_name_means_nobody(db) -> None:
    db.add(AppUser(oid=SECOND_BEN, name="Ben Alan Carter", email="other.ben@example.com"))
    db.commit()

    assert oid_for_name(db, "Ben Alan Carter") is None
    assert oid_for_name(db, "ben.carter@example.com") == oid("ben")  # the e-mail still tells them apart


def test_only_the_latest_name_of_a_person_counts(db) -> None:
    db.add(AppUser(oid=oid("ben"), name="Ben Hughes", email="ben.carter@example.com"))  # renamed
    db.commit()

    assert oid_for_name(db, "Ben Carter") is None
    assert oid_for_name(db, "Ben Hughes") == oid("ben")


def test_a_percent_or_underscore_in_what_was_typed_is_never_a_wildcard(db) -> None:
    assert oid_for_name(db, "A%ha Rao") is None  # would match "Asha" if % meant "anything"
    assert oid_for_name(db, "A_ha Rao") is None  # would match if _ meant "any one letter"
    assert oid_for_name(db, "% Rao") is None  # one word once the punctuation is ignored
    assert oid_for_name(db, "%") is None
    # Punctuation is only a separator, like the comma in "Rao, Asha".
    assert oid_for_name(db, "Asha% Rao") == oid("asha")
