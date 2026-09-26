"""Prompt layout for caching: a design constraint on
prompt layout, not an optimisation. Provider prompt caching (and local KV-cache prefix
reuse) only hits if the stable prefix -- system + persona + entity schema + constant
knowledge + rule system, unchanging within a session -- comes strictly *before* volatile
content -- retrieved (non-constant) chunks + recent history, which differ every turn. One
volatile token near the top invalidates the whole prefix, so the ordering is enforced
*structurally* here (a typed two-field dataclass), not by hoping every future call site
concatenates strings in the right order.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LayoutSections:
    """``stable`` -- unchanging within a session (entity state, constant-why knowledge
    entries). ``volatile`` -- changes every turn (retrieved/ranked knowledge, recent
    history). Both are already-rendered text blocks in their final order; this type's
    only job is to make "stable before volatile" impossible to get backwards."""

    stable: tuple[str, ...]
    volatile: tuple[str, ...]

    @property
    def stable_text(self) -> str:
        return "\n\n".join(s for s in self.stable if s)

    @property
    def volatile_text(self) -> str:
        return "\n\n".join(s for s in self.volatile if s)

    def render(self) -> str:
        stable, volatile = self.stable_text, self.volatile_text
        if stable and volatile:
            return f"{stable}\n\n{volatile}"
        return stable or volatile
