"""Every action the assistant may propose must be one the browser can apply.

The assistant never executes a write: it proposes, and clicking Apply runs the ordinary
API call from the user's own session, which is what makes its effective access exactly the
user's. That design has a seam -- a Python catalog of tools and a TypeScript switch that
executes them -- and a seam between two languages drifts silently. A tool with no case is
an assistant that proposes something the Apply button then refuses; a case with no tool is
dead code nobody can reach.
"""

from __future__ import annotations

import pathlib
import re

from core.agents.assistant_chat import _WRITE_TOOLS

_ACTIONS_TS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "web"
    / "src"
    / "features"
    / "agents"
    / "assistant-actions.ts"
)


def _browser_actions() -> set[str]:
    return set(re.findall(r'case "([a-z_]+)":', _ACTIONS_TS.read_text()))


def test_every_proposable_action_can_be_applied() -> None:
    missing = sorted(set(_WRITE_TOOLS) - _browser_actions())
    assert not missing, (
        "the assistant can propose these but the browser cannot apply them, so Apply "
        f"would refuse its own proposal: {missing}"
    )


def test_every_appliable_action_is_proposable() -> None:
    orphans = sorted(_browser_actions() - set(_WRITE_TOOLS))
    assert not orphans, (
        f"the browser can apply these but nothing can propose them (dead code): {orphans}"
    )
