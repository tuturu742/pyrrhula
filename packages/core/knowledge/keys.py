"""Activation keys derived from an entry's own title.

Retrieval fuses three lists: dense (what the turn's text resembles), sparse (words it
shares) and keyed (entries whose activation keys appear in it). The keyed list is the only
one an author controls directly and the only one that is exact, and it is the one that
answers a turn naming a thing -- a goblin, a saving throw, a Fighter -- rather than merely
resembling one.

Nobody writes those keys. Every entry a document ingest produces has none, so a rulebook
of five hundred sections arrives with the keyed list empty and retrieval left guessing
from prose: measured on a live table, a tavern scene pulled dragons, traps and
Ventriloquism, while the same corpus with keys added by hand returned the goblin, the
damage rule and the giant spiders for the turns that named them.

The titles are already there, and a section's title is what a turn calls it. So this
derives from the title and nothing else -- no model call, no corpus statistics, nothing
that could differ between two deployments holding the same text.

**Why four characters.** Keys match as case-insensitive substrings, so a short one matches
inside longer words: "elf" is in "himself", "orc" is in "torch". A three-letter key would
fire on half a transcript. Four is the shortest length that is mostly safe, and the cost
is that a very short title derives nothing -- an author can still add the key by hand,
which is a decision they can see rather than a rule they have to discover.

**What is skipped.** A document's structural furniture -- part and chapter headings,
"Introduction", "What Is This?" -- names the book, not a thing in it. Those titles match
everywhere and rank for everything: the introduction chapter of the rulebook was the
single most-retrieved entry in the first measured run, in scenes with no rules in them at
all. They derive nothing.
"""

from __future__ import annotations

import re

# Shortest key that does not fire inside ordinary words. See the module docstring.
MIN_KEY_LENGTH = 4
# Enough for a title and its parts without letting one entry flood the keyed list.
MAX_KEYS = 6

# Numbered structure: "Part 3", "Chapter VII", "Appendix A", "Section 2.1".
_NUMBERED = re.compile(r"^(part|chapter|section|appendix|book|volume)\b[\s.:]*[0-9ivxlc]*\b", re.I)
# Titles that describe the document rather than anything in it.
_STRUCTURAL = frozenset(
    {
        "about",
        "acknowledgements",
        "afterword",
        "appendix",
        "background",
        "chapter",
        "contents",
        "credits",
        "foreword",
        "glossary",
        "index",
        "introduction",
        "licence",
        "license",
        "notes",
        "overview",
        "preface",
        "part",
        "prologue",
        "purpose",
        "scope",
        "section",
        "summary",
        "table of contents",
        "terminology",
    }
)
# A title that opens like a question about the document is describing it, not naming a
# thing: "What is a Role-Playing Game?", "How to Create a Character".
_DESCRIBES_THE_DOCUMENT = re.compile(
    r"^(what|how|why|where|when)\b\s+(is|are|to|do|does|the)\b", re.I
)

# Where a title holds more than one name: "Spider, Giant Crab", "Dwarves and Elves",
# "Hold Portal (Reversible)".
_SPLIT = re.compile(r"[,;:/()]|\band\b|\bor\b", re.I)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t.,;:!?-–—")


def derive_keys(title: str) -> list[str]:
    """Activation keys for an entry with none of its own, from its title.

    The whole title first, then each name inside it, deduplicated case-insensitively and
    capped. Returns an empty list for a title that names the document rather than
    something in it, and for one too short to match safely.
    """
    cleaned = _clean(title)
    if len(cleaned) < MIN_KEY_LENGTH:
        return []
    if _NUMBERED.match(cleaned) or _DESCRIBES_THE_DOCUMENT.match(cleaned):
        return []
    if cleaned.casefold() in _STRUCTURAL:
        return []

    candidates = [cleaned, *(_clean(part) for part in _SPLIT.split(cleaned))]
    keys: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        folded = candidate.casefold()
        if len(candidate) < MIN_KEY_LENGTH or folded in seen or folded in _STRUCTURAL:
            continue
        seen.add(folded)
        keys.append(candidate)
        if len(keys) == MAX_KEYS:
            break
    return keys
