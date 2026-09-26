"""v1 ModerationProvider: allow everything. Per-tenant policy and real scanning
(authoring-time and generation-time hooks) land at G4.14."""

from __future__ import annotations

from core.ports.moderation import ModerationResult


class AllowAllModerationProvider:
    async def check(self, text: str, *, context: str) -> ModerationResult:
        return ModerationResult(allowed=True)
