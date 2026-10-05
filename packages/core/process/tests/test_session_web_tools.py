"""The web tools authorise each call against a synthesized one-tool phase, and that
phase has to name the tool being called.

It named `search` for both, so every page fetch was refused as "not available in this
phase" before it was recorded anywhere -- the newsroom's desks said pages were
unavailable and filed from snippets, on two sweeps, and nothing in a log said why.
"""

from __future__ import annotations

from core.process.session_web_tools import _web_phase


def test_the_synthesized_phase_names_the_tool_it_authorises() -> None:
    assert _web_phase("fetch").tools == ["fetch"]
    assert _web_phase("search").tools == ["search"]


def test_the_synthesized_phase_carries_nothing_else() -> None:
    """Discovery filters by `phase.tools`; visibility must stay the empty public shape,
    so a tool call can never widen what the turn itself may see."""
    phase = _web_phase("search")
    assert phase.actors == []
    assert phase.visibility.secrets == "none"
    assert phase.visibility.knowledge_classes == []
