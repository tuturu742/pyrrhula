from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.models.litellm.provider import (
    LiteLLMModelProvider,
    _apply_cache_boundary,
    _extract_cached_tokens,
)
from core.ports.model_provider import EgressDeniedError, GenerationRequest, check_egress


async def test_egress_denied_before_any_network_call() -> None:
    """Cloud purpose forbidden by tenant policy must raise before litellm is ever
    imported/called -- no network access, no API key, needed for this test."""
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        egress_policy={"generation": ["local"]},
    )

    with pytest.raises(EgressDeniedError):
        async for _ in provider.generate(req):
            pass


async def test_egress_allowed_reaches_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local model under a local-only policy must pass the gate and reach litellm --
    proven by mocking litellm.acompletion rather than making a real network call."""
    import litellm

    chunk = MagicMock()
    chunk.choices = [MagicMock(delta=MagicMock(content="hi"), finish_reason=None)]
    chunk.usage = None  # matches a real streaming chunk without stream_options.include_usage

    async def _fake_stream():
        yield chunk

    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=_fake_stream()))

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="ollama/llama3",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        egress_policy={"generation": ["local"]},
    )

    chunks = [c async for c in provider.generate(req)]
    assert chunks[0].text == "hi"


async def test_generate_passes_the_requests_api_base_through_to_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-profile endpoint override (a second local Ollama deployment, say) must
    reach LiteLLM's own ``api_base`` kwarg -- not just be accepted and silently dropped."""
    import litellm

    chunk = MagicMock()
    chunk.choices = [MagicMock(delta=MagicMock(content="hi"), finish_reason=None)]
    chunk.usage = None

    async def _fake_stream():
        yield chunk

    mock_acompletion = AsyncMock(return_value=_fake_stream())
    monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="ollama/llama3",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        api_base="http://localhost:22434",
    )

    async for _ in provider.generate(req):
        pass

    assert mock_acompletion.call_args.kwargs["api_base"] == "http://localhost:22434"


def test_default_policy_is_permissive() -> None:
    # No exception: an unset policy allows every purpose (plan: "Default policy: all
    # purposes allowed").
    check_egress("generation", "gpt-4o", {})


def test_capabilities_heuristic() -> None:
    provider = LiteLLMModelProvider()
    assert provider.capabilities("claude-opus-4-8").supports_prompt_caching is True
    assert provider.capabilities("ollama/llama3").supports_prompt_caching is False


def test_count_tokens_openai_and_fallback() -> None:
    provider = LiteLLMModelProvider()
    assert provider.count_tokens("hello world", "gpt-4o") > 0
    assert provider.count_tokens("hello world", "ollama/llama3") > 0


# ── C1.4: prompt-cache boundary marking + cached_tokens extraction ─────────────────


def test_apply_cache_boundary_marks_the_given_message() -> None:
    messages = [{"role": "system", "content": "stable stuff"}, {"role": "user", "content": "hi"}]
    marked = _apply_cache_boundary(messages, 0, supports_caching=True)

    assert marked[0]["content"] == [
        {"type": "text", "text": "stable stuff", "cache_control": {"type": "ephemeral"}}
    ]
    assert marked[1] == {"role": "user", "content": "hi"}
    assert messages[0]["content"] == "stable stuff"  # original untouched


def test_apply_cache_boundary_noop_when_unsupported_or_unset() -> None:
    messages = [{"role": "system", "content": "stable stuff"}]
    assert _apply_cache_boundary(messages, 0, supports_caching=False) == messages
    assert _apply_cache_boundary(messages, None, supports_caching=True) == messages
    assert _apply_cache_boundary(messages, 5, supports_caching=True) == messages  # out of range


def test_extract_cached_tokens_openai_shape() -> None:
    usage = MagicMock(spec=["prompt_tokens_details"])
    usage.prompt_tokens_details = MagicMock(spec=["cached_tokens"])
    usage.prompt_tokens_details.cached_tokens = 42
    assert _extract_cached_tokens(usage) == 42


def test_extract_cached_tokens_anthropic_shape() -> None:
    usage = MagicMock(spec=["cache_read_input_tokens"])
    usage.cache_read_input_tokens = 17
    assert _extract_cached_tokens(usage) == 17


def test_extract_cached_tokens_defaults_to_zero() -> None:
    assert _extract_cached_tokens(None) == 0
    assert _extract_cached_tokens(MagicMock(spec=[])) == 0


async def test_cache_boundary_applied_when_provider_supports_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    chunk = MagicMock()
    chunk.choices = [MagicMock(delta=MagicMock(content="hi"), finish_reason="stop")]
    chunk.usage = None

    async def _fake_stream():
        yield chunk

    mock_acompletion = AsyncMock(return_value=_fake_stream())
    monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="claude-opus-4-8",
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "hi"}],
        purpose="generation",
        cache_boundary_index=0,
    )
    _ = [c async for c in provider.generate(req)]

    sent_messages = mock_acompletion.call_args.kwargs["messages"]
    assert sent_messages[0]["content"] == [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}
    ]


async def test_cached_tokens_flow_into_the_yielded_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    chunk = MagicMock()
    chunk.choices = [MagicMock(delta=MagicMock(content="hi"), finish_reason="stop")]
    chunk.usage = MagicMock(spec=["cache_read_input_tokens"])
    chunk.usage.cache_read_input_tokens = 9

    async def _fake_stream():
        yield chunk

    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=_fake_stream()))

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="claude-opus-4-8", messages=[{"role": "user", "content": "hi"}], purpose="generation"
    )
    chunks = [c async for c in provider.generate(req)]

    assert chunks[0].cached_tokens == 9
