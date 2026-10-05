"""Keyword activation  — the keyed-lore convention of chat frontends,
deliberately borrowed (not reinvented): keyword matching with AND/OR/NOT secondary-key
logic, `constant` entries, and temporal activation state (`sticky`/`cooldown`/`delay`)
that solves "the tavern lore shouldn't re-inject every turn while we're in the tavern, but
also shouldn't vanish after one turn" — nothing in the plain-RAG literature does this.

Pure function over already-fetched entries, not a DB-touching module: the caller (the
assembler) fetches entries and recent-turn scan text, and is responsible for
persisting the returned ``new_state`` as part of session-derived state — that mechanism is
the job (event log + checkpoints), which doesn't exist yet. This module owns the
activation *logic* and its state *shape* only; ``prior_state``/``new_state`` are plain
JSON-able dicts specifically so the checkpoint store can hold them without this module
needing to know how.

Secondary-key logic, condensed to fit the 3-value ``logic`` CHECK constraint rather
than the classic 4-mode ``AND_ANY``/``AND_ALL``/``NOT_ANY``/``NOT_ALL``: primary
``keys[]`` always match on OR (any key present triggers); ``logic`` then governs how
``secondary_keys[]`` modifies that verdict --

- ``'AND'``: a primary match is only honoured if at least one secondary key also matched
  (secondary_keys empty => no additional constraint).
- ``'OR'``: a primary match suffices on its own; a secondary match alone also suffices.
- ``'NOT'``: a primary match is only honoured if *no* secondary key matched (secondary
  acts as an exclusion list).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from random import Random
from typing import Protocol

import regex

_REGEX_MATCH_TIMEOUT_SECONDS = 0.1


class UnsafeRegexError(ValueError):
    """A ``use_regex`` key failed to compile — raised at authoring time
    (``core.knowledge.authoring`` calls ``validate_regex_keys`` before saving), not here;
    activation itself treats an uncompilable pattern as "no match" rather than raising,
    since state loaded back from storage isn't re-validated and a turn must not fail
    because of a key an author saved before this check existed."""


def validate_regex_keys(keys: list[str]) -> None:
    """Compile-checked at save: catches a syntax error immediately, while the
    author is still looking at the form, rather than the key silently never matching."""
    for key in keys:
        try:
            re.compile(key)
        except re.error as exc:
            raise UnsafeRegexError(f"invalid regex key {key!r}: {exc}") from exc


class ActivatableEntry(Protocol):
    """The subset of ``core.knowledge.models.KnowledgeEntry`` this module needs —
    a Protocol so tests can construct lightweight fakes instead of real ORM rows."""

    id: uuid.UUID
    entry_key: str
    keys: list[str]
    secondary_keys: list[str]
    logic: str
    use_regex: bool
    constant: bool
    sticky: int | None
    cooldown: int | None
    delay: int | None
    trigger_pct: int | None
    inclusion_group: str | None
    insertion_order: int


@dataclass
class EntryActivationState:
    sticky_until_turn: int | None = None
    cooldown_until_turn: int | None = None


@dataclass
class ActivatedEntry:
    entry_id: uuid.UUID
    entry_key: str
    rank: int
    why: str
    # The author's own priority field, carried out so the budget can offer always-on
    # entries in the order they were written rather than in whatever order this turn's
    # search happened to rank them.
    insertion_order: int = 0


@dataclass
class ActivationResult:
    activated: list[ActivatedEntry]
    new_state: dict[str, EntryActivationState] = field(default_factory=dict)


def _regex_matches(pattern_text: str, text: str) -> bool:
    """Matched with a hard wall-clock timeout via the third-party ``regex`` package, not
    stdlib ``re``: CPython's ``re`` engine never releases the GIL and has no periodic
    interrupt check during a match, so a catastrophically-backtracking pattern run in a
    worker thread with a ``concurrent.futures`` timeout still hangs the whole process
    (`future.result(timeout=...)` only times out the *wait*, not the match itself; a
    first attempt at this used exactly that approach and genuinely hung the test suite).
    ``regex`` performs periodic timeout checks inside its own matching loop, so it can
    actually abort a runaway match rather than merely give up watching it.
    """
    try:
        return (
            regex.search(pattern_text, text, regex.IGNORECASE, timeout=_REGEX_MATCH_TIMEOUT_SECONDS)
            is not None
        )
    except regex.error:
        return False
    except TimeoutError:
        return False


def _key_matches(key: str, text: str, *, use_regex: bool) -> bool:
    if use_regex:
        return _regex_matches(key, text)
    # A plain key matches a whole word, not any text that contains it: a key derived from
    # a section title must not wake on "hearth" (earth), "firelight" (fire) or the
    # adjective "cold" (the cold elemental), which it did in nearly every fight turn of
    # one run. Plurals and possessives still match ("ghouls", "skeleton's"); regex keys
    # are untouched.
    pattern = r"(?<!\w)" + re.escape(key.lower()) + r"(?:s|es|'s|’s)?(?!\w)"
    return re.search(pattern, text.lower()) is not None


def _any_key_matches(keys: list[str], text: str, *, use_regex: bool) -> bool:
    return any(_key_matches(k, text, use_regex=use_regex) for k in keys)


def _keys_activate(entry: ActivatableEntry, text: str) -> bool:
    primary = _any_key_matches(entry.keys, text, use_regex=entry.use_regex)
    if not entry.secondary_keys:
        return primary
    secondary = _any_key_matches(entry.secondary_keys, text, use_regex=entry.use_regex)

    if entry.logic == "OR":
        return primary or secondary
    if entry.logic == "NOT":
        return primary and not secondary
    return primary and secondary  # 'AND' (default)


def _passes_trigger_pct(entry: ActivatableEntry, *, rng_seed: str, turn_index: int) -> bool:
    if entry.trigger_pct is None or entry.trigger_pct >= 100:
        return True
    if entry.trigger_pct <= 0:
        return False
    # Deterministic per (rng_seed, entry, turn) -- same inputs always reproduce the same
    # roll, which is what makes a session replay (INV-10) reproduce identical outcomes.
    seed_key = f"{rng_seed}:{entry.id}:{turn_index}"
    roll = Random(seed_key).random() * 100
    return roll < entry.trigger_pct


def activate_entries(
    entries: Sequence[ActivatableEntry],
    *,
    scan_text: str,
    turn_index: int,
    prior_state: dict[str, EntryActivationState],
    rng_seed: str,
) -> ActivationResult:
    new_state: dict[str, EntryActivationState] = dict(prior_state)
    candidates: list[tuple[ActivatableEntry, str]] = []

    for entry in entries:
        key = str(entry.id)
        state = prior_state.get(key, EntryActivationState())

        if entry.constant:
            candidates.append((entry, "constant"))
            continue

        if entry.delay is not None and turn_index < entry.delay:
            continue

        sticky_until = state.sticky_until_turn
        still_sticky = sticky_until is not None and turn_index <= sticky_until
        if still_sticky:
            candidates.append((entry, "sticky"))
            continue

        cooldown_until = state.cooldown_until_turn
        on_cooldown = cooldown_until is not None and turn_index < cooldown_until
        if on_cooldown:
            continue

        if not _keys_activate(entry, scan_text):
            continue

        if not _passes_trigger_pct(entry, rng_seed=rng_seed, turn_index=turn_index):
            continue

        candidates.append((entry, "keyword"))

    # inclusion_group exclusivity: only the lowest insertion_order member of a group
    # activates -- "highest rank wins", and insertion_order (author-set
    # priority) is the only ranking signal available at this stage (WRRF's real rank
    # comes later, after retrieval is fused in).
    best_in_group: dict[str, ActivatableEntry] = {}
    for entry, _why in candidates:
        if entry.inclusion_group is None:
            continue
        current_best = best_in_group.get(entry.inclusion_group)
        if current_best is None or entry.insertion_order < current_best.insertion_order:
            best_in_group[entry.inclusion_group] = entry

    activated: list[ActivatedEntry] = []
    for entry, why in candidates:
        if entry.inclusion_group is not None and best_in_group[entry.inclusion_group] is not entry:
            continue

        key = str(entry.id)
        fired_this_turn = why in ("keyword", "constant")
        if fired_this_turn and entry.sticky:
            prior_cooldown = prior_state.get(key, EntryActivationState()).cooldown_until_turn
            new_state[key] = EntryActivationState(
                sticky_until_turn=turn_index + entry.sticky,
                cooldown_until_turn=prior_cooldown,
            )
        if why == "keyword" and entry.cooldown:
            sticky_end = new_state.get(key, EntryActivationState()).sticky_until_turn
            cooldown_start = (sticky_end + 1) if sticky_end is not None else turn_index + 1
            new_state[key] = EntryActivationState(
                sticky_until_turn=sticky_end,
                cooldown_until_turn=cooldown_start + entry.cooldown,
            )

        activated.append(
            ActivatedEntry(
                entry_id=entry.id,
                entry_key=entry.entry_key,
                rank=0,
                why=why,
                insertion_order=entry.insertion_order,
            )
        )

    order_by_entry_id = {e.id: e.insertion_order for e, _ in candidates}
    activated.sort(key=lambda a: order_by_entry_id[a.entry_id])
    for i, hit in enumerate(activated):
        activated[i] = ActivatedEntry(
            entry_id=hit.entry_id, entry_key=hit.entry_key, rank=i + 1, why=hit.why
        )

    return ActivationResult(activated=activated, new_state=new_state)
