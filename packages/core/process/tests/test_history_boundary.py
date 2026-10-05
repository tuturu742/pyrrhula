"""Where the replayed transcript begins decides whether a history summary is made.

It used to be derived by counting messages against the event clock, which assumed one
message per seq. A turn that also records tool calls takes more, so "something was
dropped" was true on nearly every turn and the summariser ran 99 times on one session in
which nothing had been trimmed.
"""

from __future__ import annotations

from core.process.live_session import _tail_trim, _tail_trim_pairs


def _timeline() -> list[tuple[int, dict[str, str]]]:
    # Ten messages, seqs with gaps (tool calls between them), 100 chars each.
    return [(seq, {"role": "user", "content": "x" * 100}) for seq in range(0, 20, 2)]


def test_nothing_dropped_under_budget_means_no_boundary() -> None:
    kept = _tail_trim_pairs(_timeline(), budget=10_000)
    assert len(kept) == 10
    # The caller derives "first replayed seq" only when something was dropped.
    first_seq = kept[0][0] if kept and len(kept) < 10 else 0
    assert first_seq == 0


def test_the_boundary_is_the_first_surviving_seq() -> None:
    kept = _tail_trim_pairs(_timeline(), budget=250)
    assert [seq for seq, _ in kept] == [16, 18]
    first_seq = kept[0][0] if len(kept) < 10 else 0
    assert first_seq == 16


def test_tail_trim_keeps_the_same_messages_as_before() -> None:
    messages = [m for _seq, m in _timeline()]
    assert _tail_trim(messages, budget=250) == messages[-2:]
    assert _tail_trim(messages, budget=10_000) == messages
