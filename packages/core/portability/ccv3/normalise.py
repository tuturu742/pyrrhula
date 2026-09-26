"""V2/V3 card normalisation into one internal model.

Two spec versions, one shape. V3 added fields (`nickname`, `group_only_greetings`,
`creation_date`, decorators inside lore entries) and moved the payload under a different
chunk keyword, but the fields V2 already had mean the same things. Normalising once, here,
means `map.py` sees a single model and every downstream consumer stops asking "which
version was this".

**On vocabulary** (CLAUDE.md rule 1): the *external* spec's field names (`chara`,
`character_book`, `first_mes`) appear here because they are wire-format identifiers, the
same way `tEXt` is -- this module's whole job is to read them. They stop at this boundary:
the internal model calls them `persona_*`, `lore_entries`, `opening_message`, and nothing
past `normalise.py` ever sees the spec's nouns. That is the rule working, not an exception
to it.

**`extensions` is preserved verbatim, everywhere.** The CCv3 spec reserves it for arbitrary
data and requires implementations not to destroy it. It is carried on the card, on every
lore entry, and back out again by the export -- untouched, not merged, not normalised.
A field whose whole contract is "you don't understand this, don't break it" is one to copy
byte-for-byte and leave alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# CCv3 decorators live as `@@name` / `@@name value` lines at the head of an entry's
# content (the spec's own syntax). Both descend from ST World Info, which is why the
# mapping onto activation fields is near-mechanical rather than interpretive.
_DECORATOR_RE = re.compile(r"^@@(?P<name>[a-z_]+)(?:[ \t]+(?P<value>.*))?$", re.IGNORECASE)

# `knowledge_entry.position` is 'before_char' | 'after_char' | 'at_depth_N' . A card
# is attacker-controlled input, so a decorator's value is *validated* against that grammar
# rather than trusted: an unrecognised token means the card said something this system does
# not model, and the entry's own `position` field is the honest fallback. Without this, a
# long or malformed decorator value reaches a varchar(32) column and the import dies on a
# database error instead of on a decision.
_POSITION_RE = re.compile(r"^(?:before_char|after_char|at_depth_\d{1,4})$")


@dataclass(frozen=True)
class LoreEntry:
    """One `character_book.entries` item, normalised. Field names are Pyrrhula's
    (`keys`, `secondary_keys`, `constant`, `position`, `insertion_order`) because they map
    1:1 onto the activation fields -- the near-mechanical mapping predicted."""

    entry_key: str
    title: str
    body_md: str
    keys: list[str]
    secondary_keys: list[str]
    logic: str
    constant: bool
    position: str
    insertion_order: int
    depth: int | None
    use_regex: bool
    enabled: bool
    extensions: dict[str, Any]
    decorators: dict[str, str]


@dataclass(frozen=True)
class NormalisedCard:
    """One card, whichever spec version it arrived as. ``spec_version`` records what it
    *was*, for the loss report the importer has to write honestly."""

    spec_version: str
    name: str
    persona_md: str
    opening_message: str
    system_prompt: str
    post_history_instructions: str
    alternate_greetings: list[str]
    example_dialogue: str
    tags: list[str]
    creator: str
    lore_entries: list[LoreEntry] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def parse_decorators(content: str) -> tuple[dict[str, str], str]:
    """Splits leading `@@decorator` lines off an entry's content.

    Returns ``(decorators, remaining_content)``. Only *leading* lines are consumed: a `@@`
    further down is prose that happens to start with two at-signs, and eating it would
    silently delete someone's text."""
    decorators: dict[str, str] = {}
    lines = content.splitlines()
    consumed = 0
    for line in lines:
        match = _DECORATOR_RE.match(line.strip())
        if match is None:
            break
        decorators[match.group("name").lower()] = (match.group("value") or "").strip()
        consumed += 1
    return decorators, "\n".join(lines[consumed:]).lstrip("\n")


def _persona_from(payload: dict[str, Any]) -> str:
    """The spec spreads persona across `description`, `personality`, and `scenario`.
    Pyrrhula has one `persona_md`, so they concatenate under headings rather than being
    silently flattened -- an importer that dropped `scenario` because there was no column
    for it would be losing content the card's author wrote on purpose."""
    sections = [
        ("Description", str(payload.get("description") or "")),
        ("Personality", str(payload.get("personality") or "")),
        ("Scenario", str(payload.get("scenario") or "")),
    ]
    return "\n\n".join(f"## {label}\n{body.strip()}" for label, body in sections if body.strip())


def _normalise_lore_entry(index: int, raw: dict[str, Any]) -> LoreEntry:
    content = str(raw.get("content") or "")
    decorators, body = parse_decorators(content)

    position = str(raw.get("position") or "before_char")
    declared = decorators.get("position", "").strip()
    if declared and _POSITION_RE.match(declared):
        position = declared
    depth_raw = decorators.get("depth") or raw.get("extensions", {}).get("depth")
    depth = (
        int(depth_raw) if isinstance(depth_raw, str | int) and str(depth_raw).isdigit() else None
    )

    keys = [str(k) for k in raw.get("keys", []) if str(k).strip()]
    name = str(raw.get("name") or "").strip()
    entry_key = _slug(name or (keys[0] if keys else f"entry-{index}"))

    return LoreEntry(
        entry_key=entry_key,
        title=name or entry_key,
        body_md=body,
        keys=keys,
        secondary_keys=[str(k) for k in raw.get("secondary_keys", []) if str(k).strip()],
        # The spec's `selective` means "secondary keys must also match" -- Pyrrhula spells
        # the same thing as logic AND vs OR.
        logic="AND" if raw.get("selective") else "OR",
        constant=bool(raw.get("constant", False)),
        position=position,
        insertion_order=int(raw.get("insertion_order", 0) or 0),
        depth=depth,
        use_regex=bool(raw.get("use_regex", False)),
        enabled=bool(raw.get("enabled", True)),
        extensions=dict(raw.get("extensions") or {}),
        decorators=decorators,
    )


def normalise(keyword: str, payload: dict[str, Any]) -> NormalisedCard:
    """``keyword`` is what ``png_chunks.extract_card`` found (`ccv3`, `chara`, or `json`).

    Both versions nest their real content under ``data``; a few older exporters wrote it
    flat. Accepting both is not leniency for its own sake -- a card that loads everywhere
    else and not here is a bug users report as "your importer is broken", and they are
    right."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    assert isinstance(data, dict)

    book = data.get("character_book") or {}
    raw_entries = book.get("entries", []) if isinstance(book, dict) else []

    spec_version = str(payload.get("spec_version") or ("3.0" if keyword == "ccv3" else "2.0"))

    return NormalisedCard(
        spec_version=spec_version,
        name=str(data.get("name") or "Unnamed"),
        persona_md=_persona_from(data),
        opening_message=str(data.get("first_mes") or ""),
        system_prompt=str(data.get("system_prompt") or ""),
        post_history_instructions=str(data.get("post_history_instructions") or ""),
        alternate_greetings=[str(g) for g in data.get("alternate_greetings", [])],
        example_dialogue=str(data.get("mes_example") or ""),
        tags=[str(t) for t in data.get("tags", [])],
        creator=str(data.get("creator") or ""),
        lore_entries=[
            _normalise_lore_entry(i, e) for i, e in enumerate(raw_entries) if isinstance(e, dict)
        ],
        extensions=dict(data.get("extensions") or {}),
        raw=payload,
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "entry"
