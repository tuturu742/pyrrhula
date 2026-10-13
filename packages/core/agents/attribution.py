"""A turn speaks only for its author.

A model shown a multi-party transcript ("Name: what they said", one line per speaker)
sometimes keeps going past its own turn and writes the next speakers' lines too. Those
words are then saved under the author's name, replayed to everyone, and taken by the
participants they were attributed to as things they said -- seen live: a player answered
"I'm satisfied with my opening" about lines another player's model had written for them.

The guard is structural, not an instruction: the reply is cut at the first block that is
another participant's speech. A block is the text from a line opening with exactly a
participant's name and a colon ("Skyler:", "**Skyler:**") up to the next such line. It
counts as that participant's speech only when written in the first person ("I lean
logic"): "Reese: walk me through your case" and "Kendall: don't re-claim Sheriff" are the
author addressing someone, and list items ("- Riley: Mafia Don") are not labels at all.
What is cut, from the first such block on:

- first-person blocks for two or more different other participants -- a script;
- a block in which the writer introduces themselves as that participant
  ("Rowan: Hey everyone, I'm Rowan").

Checked against 6,382 replies from model-vs-model Mafia games: every reply it cut was a
script or a self-introduction as someone else.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# A line that opens with a speaker label: an optional quote mark, the name in optional
# bold, then a colon. A list bullet ("- ", "* ") before the name makes it a list item.
_LABEL = re.compile(
    r"^[ \t]*(?:>[ \t]*)?(?:\*\*|__)?(?P<name>[^\n:*_]{1,60}?)(?:\*\*|__)?[ \t]*:", re.MULTILINE
)
_FIRST_PERSON = re.compile(r"\bI\b|\bI['’](?:m|ve|ll|d)\b|\b(?:[Mm]y|[Mm]e|[Mm]ine)\b")


@dataclass(frozen=True)
class Attribution:
    text: str
    # The participants the cut text was written for, in the order they appeared; empty
    # when nothing was cut.
    spoken_for: tuple[str, ...]


def keep_own_words(text: str, author: str, participants: Iterable[str]) -> Attribution:
    """``text`` cut before the first block it writes for another participant, when it is
    a script (see the module docstring). ``participants`` may include the author."""
    others: dict[str, str] = {}  # a label, lowercased (full name or first name) -> name
    for name in participants:
        clean = name.strip()
        if not clean or clean.lower() == author.strip().lower():
            continue
        others.setdefault(clean.lower(), clean)
        first = clean.lower().split(" ")[0]
        if len(first) >= 3:
            others.setdefault(first, clean)

    labels = [
        (m.start(), m.end(), others[m.group("name").strip().lower()])
        for m in _LABEL.finditer(text)
        if m.group("name").strip().lower() in others
    ]
    # (where it starts, whose label, the block's text up to the next label)
    blocks = [
        (start, owner, text[end : labels[i + 1][0] if i + 1 < len(labels) else len(text)])
        for i, (start, end, owner) in enumerate(labels)
    ]
    speech = [b for b in blocks if _FIRST_PERSON.search(b[2])]
    impostor = [
        b
        for b in blocks
        if re.search(rf"\bI(?:'m|’m| am)\s+{re.escape(b[1].split(' ')[0])}\b", b[2], re.I)
    ]
    if len({owner for _start, owner, _block in speech}) >= 2:
        cut_at = speech[0][0]
    elif impostor:
        cut_at = impostor[0][0]
    else:
        return Attribution(text, ())
    spoken_for = tuple(dict.fromkeys(owner for start, owner, _block in blocks if start >= cut_at))
    return Attribution(text[:cut_at].rstrip(), spoken_for)
