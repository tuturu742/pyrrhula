"""A policy-driven local `ModerationProvider` (G4.14).

Not a real classifier and not pretending to be one: it flags text containing any of the
tenant's configured category terms. That makes per-tenant policy *testable* -- the same
text passes for a permissive tenant and blocks for a strict one, which is the acceptance
criterion -- without a network call, an API key, or a model whose verdicts drift between
runs.

A provider-backed implementation is a drop-in behind the same port. This one is honest
about being a fixture-grade default: it will not catch anything a category term does not
literally name, and `AllowAllModerationProvider` remains what a deployment gets until it
configures a policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.ports.moderation import ModerationResult


@dataclass
class KeywordModerationProvider:
    categories: dict[str, tuple[str, ...]] = field(default_factory=dict)

    async def check(self, text: str, *, context: str) -> ModerationResult:
        del context  # the same terms apply at authoring and at generation
        lowered = text.lower()
        hits = [
            category
            for category, terms in sorted(self.categories.items())
            if any(term.lower() in lowered for term in terms)
        ]
        return ModerationResult(allowed=not hits, reasons=hits)
