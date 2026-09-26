"""The allow-everything ModerationProvider: the default when no classifier is
configured. Real scanning is ``model_backed``; the hooks that call either are in
``core.moderation``."""

from __future__ import annotations

from core.ports.moderation import ModerationResult


class AllowAllModerationProvider:
    async def check(self, text: str, *, context: str) -> ModerationResult:
        return ModerationResult(allowed=True)
