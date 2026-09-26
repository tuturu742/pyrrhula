"""Deterministic test/demo ``ModelProvider``: echoes the last user message back, split
into word-sized chunks to exercise real streaming semantics. **Not a production
adapter.** Exists so the walking skeleton — and its automated tests, and anyone doing
a live demo without Ollama running or API keys configured — can prove the
agent-runtime -> streaming -> persistence pipeline deterministically.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TypeVar

from pydantic import BaseModel

from core.ports.model_provider import (
    Capabilities,
    Chunk,
    GenerationRequest,
    check_egress,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class EchoModelProvider:
    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        check_egress(req.purpose, req.model, req.egress_policy)

        last_user = next(
            (m["content"] for m in reversed(req.messages) if m.get("role") == "user"), ""
        )
        reply = f"echo: {last_user}"
        words = reply.split(" ")
        for i, word in enumerate(words):
            text = word + (" " if i < len(words) - 1 else "")
            yield Chunk(text=text)
            await asyncio.sleep(0)  # cooperative yield -- real streaming has gaps too
        yield Chunk(text="", finish_reason="stop")

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        raise NotImplementedError("EchoModelProvider is generate()-only")

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=False, supports_prompt_caching=False
        )
