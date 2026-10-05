"""A plain activation key matches at a word start, not anywhere in the text.

Keys derived from rulebook section titles are single words, and matching them as bare
substrings woke "Elemental, Earth" on "hearth", "Elemental, Fire" on "firelight" and
"Elemental, Cold" on the adjective "cold", in nearly every fight turn of one run.
"""

from __future__ import annotations

from core.knowledge.activation import _key_matches


def test_a_key_does_not_wake_inside_another_word() -> None:
    assert not _key_matches("earth", "They gathered by the hearth.", use_regex=False)
    assert not _key_matches("fire", "The firelight flickered.", use_regex=False)
    assert not _key_matches("cold", "She would scold him.", use_regex=False)


def test_a_key_still_wakes_on_plurals_possessives_and_case() -> None:
    assert _key_matches("ghoul", "Two ghouls shambled closer.", use_regex=False)
    assert _key_matches("skeleton", "The skeleton's blade.", use_regex=False)
    assert _key_matches("Earth", "The earth shook.", use_regex=False)
    assert _key_matches("holy water", "a flask of holy water", use_regex=False)


def test_regex_keys_are_untouched() -> None:
    assert _key_matches("ear.h", "the hearth", use_regex=True)
