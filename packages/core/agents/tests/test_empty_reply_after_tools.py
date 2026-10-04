"""An empty reply is a failed generation -- except right after the turn acted through a
tool, when it is a finished turn.

Seen live with gpt-5.6-luna on two deployments: the model created the work item through
the tool, then answered the tool result with nothing. The runtime failed the turn, both
retries reproduced it, and the session paused over work that was already done.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from core.agents.runtime import AllRetriesExhaustedError, _call_provider_with_retry
from core.ports.model_provider import Chunk


@dataclass
class _Profile:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    provider: str = "fake"
    model: str = "fake-1"
    api_base: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    credential_ref: str | None = None


class _SilentProvider:
    async def generate(self, _req: Any):  # noqa: ANN202
        if False:  # pragma: no cover -- an async generator that yields nothing
            yield Chunk(text="")

    def count_tokens(self, text: str, _model: str) -> int:
        return len(text.split())


async def _call(*, allow_empty: bool) -> tuple[str, list[Any], Any, str]:
    return await _call_provider_with_retry(
        _Profile(),
        None,
        [{"role": "user", "content": "go on"}],
        None,
        "generation",
        lambda _name: _SilentProvider(),
        None,
        2,
        None,
        allow_empty=allow_empty,
    )


async def test_an_empty_reply_still_fails_a_turn_that_did_nothing() -> None:
    with pytest.raises(AllRetriesExhaustedError):
        await _call(allow_empty=False)


async def test_an_empty_reply_after_a_tool_call_is_a_finished_turn() -> None:
    content, tool_calls, _usage, _reasoning = await _call(allow_empty=True)
    assert content == ""
    assert tool_calls == []
