"""ModerationProvider port. v1 is a no-op allow-all; real providers plug
in at Phase 4 without changing the pre/post hook call sites."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ModerationResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


class ModerationProvider(Protocol):
    async def check(self, text: str, *, context: str) -> ModerationResult: ...
