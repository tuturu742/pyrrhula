"""The flow's number is a floor; the model's window raises it; the connection overrides.

Every flow in every pack was authored against models with a small window, so without this
a frontier model is handed the same three thousand tokens as an eight-billion-parameter
model on a laptop -- and nobody can see the difference, because the flow is portable and
the connection is chosen later by someone who never reads it."""

from __future__ import annotations

from core.assembler.knowledge_budget import (
    KNOWLEDGE_BUDGET_PARAM,
    KNOWLEDGE_CONTEXT_SHARE,
    KNOWLEDGE_TOKENS_CAP,
    effective_knowledge_tokens,
)


def test_an_unknown_window_keeps_exactly_what_the_flow_asked_for() -> None:
    """The behaviour every deployment has today, and the right thing to be wrong towards:
    an OpenAI-compatible endpoint serving a model LiteLLM has never heard of is common."""
    assert effective_knowledge_tokens(3000, context_window=None) == 3000
    assert effective_knowledge_tokens(3000, context_window=0) == 3000


def test_a_small_window_does_not_shrink_the_flows_number() -> None:
    """A local model's window is small enough that its share falls under what the flow
    asked for. The flow wins -- this raises budgets, it never lowers them."""
    assert effective_knowledge_tokens(3000, context_window=16384) == 3000


def test_a_large_window_raises_the_budget_to_its_share() -> None:
    window = 32000
    assert effective_knowledge_tokens(3000, context_window=window) == int(
        window * KNOWLEDGE_CONTEXT_SHARE
    )


def test_a_very_large_window_is_capped() -> None:
    """A million-token model does not want a hundred and fifty thousand tokens of
    retrieved handbook on every turn of every persona."""
    assert effective_knowledge_tokens(3000, context_window=1_000_000) == KNOWLEDGE_TOKENS_CAP


def test_the_connections_own_number_wins_outright() -> None:
    """An operator who has measured their own model should not have to argue with a
    heuristic -- the same shape as history_char_budget."""
    params = {KNOWLEDGE_BUDGET_PARAM: 500}
    assert effective_knowledge_tokens(3000, params=params, context_window=1_000_000) == 500
    assert effective_knowledge_tokens(3000, params={KNOWLEDGE_BUDGET_PARAM: "12000"}) == 12000


def test_an_unusable_override_falls_through_rather_than_failing_the_turn() -> None:
    for raw in ("", "not a number", 0, -1, None):
        assert effective_knowledge_tokens(3000, params={KNOWLEDGE_BUDGET_PARAM: raw}) == 3000, raw
