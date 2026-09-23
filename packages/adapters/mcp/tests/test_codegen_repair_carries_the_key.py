"""Every request the codegen makes is authenticated, including the repair.

The first call passed ``api_key``; the format-repair retry built thirty lines below it
did not. So when a model's first answer carried no ``===FILE:`` blocks -- which is the
only time the repair fires -- the retry went out keyless, a hosted provider refused it on
authentication, and the delegation fell back to the scaffold. The scaffold writes
placeholder files named after the work item, so a Rust repository acquired a ``.js`` file
named after a sentence, and the reviewer rejected a pull request whose real fault was
three layers up.

It reads as "the model declined", intermittently, because it only happens on the repair
path. The paragraph above the bug already described this exact failure arriving through a
refused temperature parameter; this asserts the shape rather than the symptom.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from adapters.mcp.codegen import make_model_codegen
from core.ports.model_provider import GenerationRequest


class _RecordingProvider:
    """Answers the first call with prose (forcing the repair) and the second with a file
    block, recording what each request carried."""

    def __init__(self) -> None:
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest):  # noqa: ANN201
        self.requests.append(request)
        text = (
            "Sure, I would start by refactoring the module."
            if len(self.requests) == 1
            else "===FILE: src/a.py===\nprint('hi')\n===END===\n"
        )
        yield SimpleNamespace(text=text)


@pytest.mark.asyncio
async def test_the_repair_request_carries_the_same_api_key(monkeypatch: Any) -> None:
    provider = _RecordingProvider()
    monkeypatch.setattr(
        "adapters.models.litellm.provider.LiteLLMModelProvider",
        lambda *a, **k: provider,
    )

    codegen = make_model_codegen(
        model="anthropic/claude-sonnet-5", api_base=None, params={}, api_key="sk-test-key"
    )
    out = await codegen({"title": "t"}, "brief", {"src/a.py": "old"}, None)

    assert out.files == {"src/a.py": "print('hi')\n"}
    assert len(provider.requests) == 2, "the repair did not fire; the test proves nothing"
    first, repair = provider.requests
    assert first.api_key == "sk-test-key"
    assert repair.api_key == "sk-test-key", (
        "the repair went out keyless -- a hosted provider refuses it and the delegation "
        "falls back to writing placeholder files"
    )
