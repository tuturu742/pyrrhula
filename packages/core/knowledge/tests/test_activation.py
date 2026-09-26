"""Acceptance criteria: table-driven tests for keyword activation. Pure unit tests
(no DB) — ``activate_entries`` is a pure function over already-fetched entries.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

import pytest

from core.knowledge.activation import (
    EntryActivationState,
    UnsafeRegexError,
    activate_entries,
    validate_regex_keys,
)


@dataclass
class FakeEntry:
    entry_key: str
    keys: list[str] = field(default_factory=list)
    secondary_keys: list[str] = field(default_factory=list)
    logic: str = "AND"
    use_regex: bool = False
    constant: bool = False
    sticky: int | None = None
    cooldown: int | None = None
    delay: int | None = None
    trigger_pct: int | None = None
    inclusion_group: str | None = None
    insertion_order: int = 0
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def _activate(entries, *, scan_text="", turn_index=0, prior_state=None, rng_seed="session-1"):
    return activate_entries(
        entries,
        scan_text=scan_text,
        turn_index=turn_index,
        prior_state=prior_state or {},
        rng_seed=rng_seed,
    )


# ── primary key matching ──────────────────────────────────────────────────────────


def test_any_primary_key_activates_or_semantics() -> None:
    entry = FakeEntry("grappling", keys=["grapple", "wrestle"])
    result = _activate([entry], scan_text="I try to wrestle the orc.")
    assert [a.entry_key for a in result.activated] == ["grappling"]


def test_no_key_match_does_not_activate() -> None:
    entry = FakeEntry("grappling", keys=["grapple"])
    result = _activate([entry], scan_text="I cast a fireball.")
    assert result.activated == []


# ── secondary_keys logic ──────────────────────────────────────────────────────────


def test_logic_and_requires_both_primary_and_secondary() -> None:
    entry = FakeEntry("combo", keys=["sword"], secondary_keys=["fire"], logic="AND")
    assert _activate([entry], scan_text="a sword").activated == []
    assert _activate([entry], scan_text="a sword of fire").activated != []


def test_logic_or_either_primary_or_secondary_suffices() -> None:
    entry = FakeEntry("combo", keys=["sword"], secondary_keys=["fire"], logic="OR")
    assert _activate([entry], scan_text="a sword").activated != []
    assert _activate([entry], scan_text="fire everywhere").activated != []
    assert _activate([entry], scan_text="nothing relevant").activated == []


def test_logic_not_secondary_excludes() -> None:
    entry = FakeEntry("combo", keys=["sword"], secondary_keys=["broken"], logic="NOT")
    assert _activate([entry], scan_text="a sword").activated != []
    assert _activate([entry], scan_text="a broken sword").activated == []


def test_and_with_empty_secondary_keys_is_just_primary() -> None:
    entry = FakeEntry("solo", keys=["torch"], secondary_keys=[], logic="AND")
    assert _activate([entry], scan_text="a lit torch").activated != []


# ── regex keys ─────────────────────────────────────────────────────────────────────


def test_regex_key_matches_pattern() -> None:
    entry = FakeEntry("numbered", keys=[r"\bd(4|6|8|10|12|20)\b"], use_regex=True)
    assert _activate([entry], scan_text="roll a d20").activated != []
    assert _activate([entry], scan_text="roll a d100").activated == []


def test_invalid_regex_key_never_matches_but_does_not_raise() -> None:
    entry = FakeEntry("broken", keys=["("], use_regex=True)
    result = _activate([entry], scan_text="anything (")
    assert result.activated == []


def test_catastrophic_backtracking_pattern_times_out_instead_of_hanging() -> None:
    entry = FakeEntry("evil", keys=[r"(a+)+$"], use_regex=True)
    text = "a" * 40 + "!"  # classic ReDoS trigger: no trailing match, exponential backtrack
    start = time.monotonic()
    result = _activate([entry], scan_text=text)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0  # bounded by the 0.1s per-match timeout, not hanging
    assert result.activated == []


def test_validate_regex_keys_rejects_invalid_pattern_at_authoring_time() -> None:
    with pytest.raises(UnsafeRegexError):
        validate_regex_keys(["("])


def test_validate_regex_keys_accepts_valid_patterns() -> None:
    validate_regex_keys([r"\bd20\b", "grapple"])  # does not raise


# ── constant entries ───────────────────────────────────────────────────────────────


def test_constant_entry_activates_regardless_of_keys() -> None:
    entry = FakeEntry("always-on", keys=["never-matches-anything"], constant=True)
    result = _activate([entry], scan_text="completely unrelated text")
    assert [a.entry_key for a in result.activated] == ["always-on"]


# ── sticky ──────────────────────────────────────────────────────────────────────────


def test_sticky_keeps_entry_active_for_n_turns_after_firing() -> None:
    entry = FakeEntry("torch-lit", keys=["torch"], sticky=2)

    turn0 = _activate([entry], scan_text="light the torch", turn_index=0)
    assert turn0.activated != []

    turn1 = _activate([entry], scan_text="unrelated", turn_index=1, prior_state=turn0.new_state)
    assert turn1.activated != []  # still sticky, keys don't need to match

    turn2 = _activate([entry], scan_text="unrelated", turn_index=2, prior_state=turn1.new_state)
    assert turn2.activated != []  # sticky_until_turn == 2, inclusive

    turn3 = _activate([entry], scan_text="unrelated", turn_index=3, prior_state=turn2.new_state)
    assert turn3.activated == []  # sticky window has ended


# ── cooldown ────────────────────────────────────────────────────────────────────────


def test_cooldown_blocks_refire_for_n_turns() -> None:
    entry = FakeEntry("echo", keys=["shout"], cooldown=2)

    turn0 = _activate([entry], scan_text="shout loudly", turn_index=0)
    assert turn0.activated != []

    turn1 = _activate([entry], scan_text="shout again", turn_index=1, prior_state=turn0.new_state)
    assert turn1.activated == []  # on cooldown despite matching keys

    turn2 = _activate([entry], scan_text="shout again", turn_index=2, prior_state=turn1.new_state)
    assert turn2.activated == []

    turn3 = _activate([entry], scan_text="shout again", turn_index=3, prior_state=turn2.new_state)
    assert turn3.activated != []  # cooldown has elapsed


def test_sticky_then_cooldown_chain_correctly() -> None:
    entry = FakeEntry("combo", keys=["shout"], sticky=1, cooldown=2)

    turn0 = _activate([entry], scan_text="shout", turn_index=0)
    assert turn0.activated != []  # fires via keyword
    turn1 = _activate([entry], scan_text="", turn_index=1, prior_state=turn0.new_state)
    assert turn1.activated != []  # still sticky (until_turn=1)
    turn2 = _activate([entry], scan_text="shout", turn_index=2, prior_state=turn1.new_state)
    assert turn2.activated == []  # sticky ended, now on cooldown
    turn3 = _activate([entry], scan_text="shout", turn_index=3, prior_state=turn2.new_state)
    assert turn3.activated == []  # still on cooldown
    turn4 = _activate([entry], scan_text="shout", turn_index=4, prior_state=turn3.new_state)
    assert turn4.activated != []  # cooldown elapsed


# ── delay ───────────────────────────────────────────────────────────────────────────


def test_delay_gates_activation_before_turn_n() -> None:
    entry = FakeEntry("late-bloomer", keys=["ancient"], delay=3)

    early = _activate([entry], scan_text="the ancient ruins", turn_index=2)
    assert early.activated == []

    on_time = _activate([entry], scan_text="the ancient ruins", turn_index=3)
    assert on_time.activated != []


# ── trigger_pct ─────────────────────────────────────────────────────────────────────


def test_trigger_pct_zero_never_activates() -> None:
    entry = FakeEntry("rare", keys=["gem"], trigger_pct=0)
    result = _activate([entry], scan_text="a shiny gem")
    assert result.activated == []


def test_trigger_pct_hundred_always_activates() -> None:
    entry = FakeEntry("common", keys=["gem"], trigger_pct=100)
    result = _activate([entry], scan_text="a shiny gem")
    assert result.activated != []


def test_trigger_pct_is_deterministic_given_same_seed_and_turn() -> None:
    entry = FakeEntry("maybe", keys=["gem"], trigger_pct=50)
    first = _activate([entry], scan_text="a shiny gem", turn_index=7, rng_seed="session-x")
    second = _activate([entry], scan_text="a shiny gem", turn_index=7, rng_seed="session-x")
    assert [a.entry_key for a in first.activated] == [a.entry_key for a in second.activated]


def test_trigger_pct_can_differ_across_seeds() -> None:
    entry = FakeEntry("maybe", keys=["gem"], trigger_pct=50)
    outcomes = {
        tuple(
            a.entry_key for a in _activate([entry], scan_text="gem", rng_seed=f"seed-{i}").activated
        )
        for i in range(20)
    }
    # With 20 different seeds and a 50% roll, both outcomes (fired / not fired) should
    # appear at least once -- proves it's not accidentally always-true or always-false.
    assert len(outcomes) == 2


# ── inclusion_group ─────────────────────────────────────────────────────────────────


def test_inclusion_group_only_lowest_insertion_order_wins() -> None:
    a = FakeEntry("option-a", keys=["choice"], inclusion_group="pick-one", insertion_order=5)
    b = FakeEntry("option-b", keys=["choice"], inclusion_group="pick-one", insertion_order=1)
    result = _activate([a, b], scan_text="make a choice")
    assert [x.entry_key for x in result.activated] == ["option-b"]


def test_inclusion_group_does_not_affect_entries_outside_the_group() -> None:
    a = FakeEntry("option-a", keys=["choice"], inclusion_group="pick-one", insertion_order=1)
    b = FakeEntry("option-b", keys=["choice"], inclusion_group="pick-one", insertion_order=5)
    unrelated = FakeEntry("unrelated", keys=["choice"], inclusion_group=None)
    result = _activate([a, b, unrelated], scan_text="make a choice")
    assert {x.entry_key for x in result.activated} == {"option-a", "unrelated"}


# ── state round-trip / replay ────────────────────────────────────────────────────────


def test_state_round_trips_as_plain_dataclasses_across_calls() -> None:
    entry = FakeEntry("torch-lit", keys=["torch"], sticky=1)
    turn0 = _activate([entry], scan_text="light the torch", turn_index=0)
    assert isinstance(turn0.new_state[str(entry.id)], EntryActivationState)

    turn1 = _activate([entry], scan_text="", turn_index=1, prior_state=turn0.new_state)
    assert turn1.activated != []


def test_replaying_the_same_turn_sequence_reproduces_identical_outcomes() -> None:
    """INV-10 at the granularity activation owns: given the same inputs at every step
    (including the rng_seed), replaying a sequence of activate_entries calls must
    reproduce identical activation outcomes at every turn."""
    entry = FakeEntry("maybe", keys=["gem"], trigger_pct=50, sticky=1, cooldown=1)

    def _run() -> list[list[str]]:
        state: dict[str, EntryActivationState] = {}
        outcomes = []
        for turn in range(6):
            result = _activate(
                [entry],
                scan_text="a gem",
                turn_index=turn,
                prior_state=state,
                rng_seed="replay-seed",
            )
            state = result.new_state
            outcomes.append([a.entry_key for a in result.activated])
        return outcomes

    assert _run() == _run()
