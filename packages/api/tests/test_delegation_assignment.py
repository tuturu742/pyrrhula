"""Who builds which work item.

Delegation used to deal items out round-robin in persona-id order, so a supervisor could
not match work to a particular dev -- the seniority of the assigned persona decides which
model writes the code (worker.delegation._codegen_profile_for), which made that ordering
load-bearing by accident. The swdev work_item schema has always carried an `assignee`
field; these cover honouring it.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.routes.sessions import _select_assignee


@dataclass
class _P:
    name: str
    key: str


SENIOR, MIDDLE, JUNIOR = _P("Senior", "senior"), _P("Middle", "middle"), _P("Junior", "junior")
DEVS = [SENIOR, MIDDLE, JUNIOR]
BY_NAME = {p.name.lower(): p for p in DEVS} | {p.key.lower(): p for p in DEVS}


def test_a_named_assignee_wins_over_position() -> None:
    """Index 0 would round-robin to SENIOR; the name must override it."""
    chosen, missed = _select_assignee("Junior", DEVS, BY_NAME, 0)
    assert chosen is JUNIOR
    assert missed is False


def test_matching_is_case_and_whitespace_insensitive() -> None:
    """A plan writes prose, not identifiers."""
    for written in ("junior", "  JUNIOR ", "Junior"):
        chosen, missed = _select_assignee(written, DEVS, BY_NAME, 0)
        assert chosen is JUNIOR, written
        assert missed is False


def test_the_persona_key_works_as_well_as_the_display_name() -> None:
    chosen, _ = _select_assignee("middle", DEVS, BY_NAME, 0)
    assert chosen is MIDDLE


def test_no_assignee_is_left_for_the_facilitator_to_decide() -> None:
    """An unnamed item is undecided, not positional.

    This used to return the round robin, which meant the *ordering of persona ids*
    picked who built each item -- and a tiered roster whose tiers are chosen by index
    is not a roster, it is a queue. The worker asks the session's facilitator instead
    (and keeps the round robin as its own fallback when that is unavailable), which it
    cannot do if a choice has already been made here.
    """
    for index in range(4):
        assert _select_assignee("", DEVS, BY_NAME, index) == (None, False)


def test_an_unknown_name_is_reported_but_still_gets_built() -> None:
    """A typo in a plan must not stall the item, and must not be silent either."""
    chosen, missed = _select_assignee("Priya", DEVS, BY_NAME, 1)
    assert chosen is MIDDLE, "falls back to the round robin"
    assert missed is True, "the unresolved name has to be reported"


def test_no_devs_on_the_roster_assigns_nobody() -> None:
    assert _select_assignee("Senior", [], {}, 0) == (None, True)
    assert _select_assignee("", [], {}, 0) == (None, False)
