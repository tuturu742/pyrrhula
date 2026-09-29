"""Derived activation keys: what a title is worth as a key, and what it is not.

The cases are real titles from the Basic Fantasy rulebook, which is the corpus that
produced the problem -- five hundred sections, no keys on any of them, and a keyed list
that therefore never fired."""

from __future__ import annotations

import pytest

from core.knowledge.keys import MAX_KEYS, derive_keys


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Goblin", ["Goblin"]),
        ("Initiative", ["Initiative"]),
        ("Saving Throws", ["Saving Throws"]),
        # More than one name in one title: each is worth a key of its own, because a turn
        # will say "a giant crab spider", never the catalogue's inverted form.
        ("Spider, Giant Crab", ["Spider, Giant Crab", "Spider", "Giant Crab"]),
        ("Dwarves and Elves", ["Dwarves and Elves", "Dwarves", "Elves"]),
        ("Hold Portal (Reversible)", ["Hold Portal (Reversible)", "Hold Portal", "Reversible"]),
    ],
)
def test_a_title_that_names_a_thing_becomes_keys(title: str, expected: list[str]) -> None:
    assert derive_keys(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Part 3: Spells",
        "PART 6:  MONSTERS",
        "Chapter VII",
        "Appendix A",
        "Introduction",
        "Table of Contents",
        "What is This?",
        "What is a Role-Playing Game?",
        "How to Create a Player Character",
        "Why the Rules Work",
    ],
)
def test_a_title_that_names_the_document_derives_nothing(title: str) -> None:
    """These match everywhere and rank for everything: the rulebook's introduction was the
    most-retrieved entry of the first measured run, in scenes with no rules in them."""
    assert derive_keys(title) == []


@pytest.mark.parametrize("title", ["Orc", "Elf", "XP", "", "   "])
def test_a_title_too_short_to_match_safely_derives_nothing(title: str) -> None:
    """Keys match as substrings, so "elf" fires inside "himself" and "orc" inside
    "torch". An author can still add a short key deliberately."""
    assert derive_keys(title) == []


def test_keys_are_capped_and_deduplicated() -> None:
    title = "Alpha, Beta, Gamma, Delta, Epsilon, Zeta, Eta, Theta"
    keys = derive_keys(title)
    assert len(keys) == MAX_KEYS
    assert keys[0] == title
    assert len({k.casefold() for k in keys}) == len(keys)


def test_a_repeated_name_is_not_a_second_key() -> None:
    assert derive_keys("Charm Person") == ["Charm Person"]
    assert derive_keys("Sleep, Sleep") == ["Sleep, Sleep", "Sleep"]


def test_structural_words_inside_a_title_are_not_keys_of_their_own() -> None:
    """The whole title still names something; the furniture word inside it does not."""
    assert derive_keys("Movement and Encumbrance") == [
        "Movement and Encumbrance",
        "Movement",
        "Encumbrance",
    ]
    assert derive_keys("Treasure, Overview") == ["Treasure, Overview", "Treasure"]
