"""Reranker port (D9, §13.3, A1.7): class-blind cross-encoder rerank, in-process (no API
call). "Class-blind by design" isn't a policy enforced elsewhere that this port could
still violate — it's structural: the signature below has no ``class_`` parameter at all,
so an adapter has nothing to condition on even if it wanted to. Class priority lives in
bucket allocation (A1.6), where it's auditable; baking it into the reranker would hide the
policy inside a model instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: uuid.UUID
    text: str


@dataclass(frozen=True)
class RerankResult:
    chunk_id: uuid.UUID
    score: float


class Reranker(Protocol):
    @property
    def model_name(self) -> str: ...

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> list[RerankResult]: ...
