"""Free-text search: every word typed must be found, in any order, ignoring punctuation."""

from app.shared.search_words import MAX_WORDS, matches_all_words, search_words


def test_words_are_lower_cased_and_split_on_whitespace():
    assert search_words("Orifice  PRESSURE\t1 plug") == ["orifice", "pressure", "1", "plug"]


def test_punctuation_around_a_word_is_ignored_but_inside_it_is_kept():
    assert search_words('plug, "2mm" (1/8)') == ["plug", "2mm", "1/8"]


def test_blank_or_punctuation_only_text_has_no_words():
    assert search_words(None) == []
    assert search_words("   ") == []
    assert search_words(" , . ; ") == []


def test_a_repeated_word_counts_once_and_the_list_is_capped():
    assert search_words("plug PLUG plug") == ["plug"]
    assert len(search_words(" ".join(f"w{i}" for i in range(30)))) == MAX_WORDS


def test_all_words_must_be_found_in_any_order():
    description = 'ORIFICE, PRESSURE 1 PLUG, 2MM 1/8"'
    assert matches_all_words(description, search_words("orifice pressure 1 plug 2mm"))
    assert matches_all_words(description, search_words("2mm plug orifice"))
    assert not matches_all_words(description, search_words("orifice gasket"))


def test_a_word_may_be_part_of_a_longer_word():
    assert matches_all_words("GEAR GIRTH BALLMILL", search_words("gear girth ball mill"))


def test_no_words_matches_everything():
    assert matches_all_words("anything", [])
