import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from adapters.models.litellm.provider import (
    LiteLLMModelProvider,
    _apply_cache_boundary,
    _extract_cached_tokens,
    _first_json_object,
    _rejects_schema_constrained_output,
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


# ── structured output on endpoints that refuse json_schema ──────────────────────────
class _Draft(BaseModel):
    text: str


def test_first_json_object_tolerates_fences_and_prose() -> None:
    """Unconstrained output is not guaranteed to be bare JSON, so the fallback does what
    the refused response_format would have done."""
    assert _first_json_object('{"text": "a"}') == '{"text": "a"}'
    assert json.loads(_first_json_object('```json\n{"text": "a"}\n```')) == {"text": "a"}
    assert json.loads(_first_json_object('Sure!\n{"text": "a"}\nHope that helps.')) == {"text": "a"}
    # Nothing parseable degrades to the raw text, which then fails validation loudly
    # rather than silently returning a wrong object.
    assert _first_json_object("no json here") == "no json here"


def test_rejects_schema_constrained_output_matches_the_parameter_not_a_vendor() -> None:
    assert _rejects_schema_constrained_output(
        Exception("OpenAIException - This response_format type is unavailable now")
    )
    assert _rejects_schema_constrained_output(Exception("unsupported json_schema"))
    assert not _rejects_schema_constrained_output(Exception("rate limit exceeded"))


async def test_generate_structured_falls_back_when_json_schema_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeepSeek (and other OpenAI-compatible endpoints) reject `json_schema` while
    accepting `json_object`. Every structured call failed there -- persona drafting,
    knowledge drafting -- surfacing as "Assistant call failed". The retry must ask for
    the same object with the schema in the prompt and still return a validated instance.
    """
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            raise Exception("OpenAIException - This response_format type is unavailable now")
        reply = MagicMock()
        reply.choices = [MagicMock(message=MagicMock(content='```json\n{"text": "drafted"}\n```'))]
        return reply

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/deepseek-flash",
        messages=[{"role": "user", "content": "draft me a persona"}],
        purpose="generation",
        api_base="https://api.deepseek.com/v1",
    )

    result = await provider.generate_structured(req, _Draft)

    assert result.text == "drafted"
    assert len(calls) == 2, "expected one refused attempt then one fallback"
    assert calls[0]["response_format"] is _Draft
    assert calls[1]["response_format"] == {"type": "json_object"}
    # The schema has to travel in the prompt, or the model has nothing to conform to.
    assert "json schema" in calls[1]["messages"][-1]["content"].lower()
    assert "text" in calls[1]["messages"][-1]["content"]
    # api_base and friends must survive the retry.
    assert calls[1]["api_base"] == "https://api.deepseek.com/v1"


async def test_generate_structured_does_not_retry_unrelated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate limit is not a capability gap; retrying it would double the damage."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        raise Exception("rate limit exceeded")

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/deepseek-flash",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
    )

    with pytest.raises(Exception, match="rate limit"):
        await provider.generate_structured(req, _Draft)
    assert len(calls) == 1


# ── connection params passthrough + the tools/reasoning fallback ─────────────────────
def test_merge_connection_params_precedence_and_reserved_keys() -> None:
    """A connection's params fill gaps and never override what the platform manages.
    None counts as unset, so a connection-level temperature default lands when the
    request has no opinion -- and a request's explicit value always wins."""
    from adapters.models.litellm.provider import _merge_connection_params

    merged = _merge_connection_params(
        {"temperature": None, "max_tokens": 900},
        {
            "temperature": 0.3,  # fills the gap
            "max_tokens": 5,  # loses to the explicit request value
            "reasoning_effort": "none",  # passes through
            "api_key": "sk-steal",  # managed: dropped
            "model": "other/model",  # managed: dropped
            "tools": [],  # managed: dropped
        },
    )
    assert merged["temperature"] == 0.3
    assert merged["max_tokens"] == 900
    assert merged["reasoning_effort"] == "none"
    assert "api_key" not in merged and "model" not in merged and "tools" not in merged


async def test_generate_retries_without_reasoning_when_tools_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-5.6-luna's chat-completions endpoint refuses function tools while a
    reasoning_effort is in play and says to set it to 'none'. Verified live: the default
    call fails, the same call with reasoning_effort='none' succeeds. The retry must do
    exactly that -- once, and only for this failure."""
    import litellm

    from core.ports.model_provider import ToolSpec

    calls: list[dict] = []

    chunk = MagicMock()
    chunk.choices = [
        MagicMock(delta=MagicMock(content="hi", tool_calls=None), finish_reason="stop")
    ]
    chunk.usage = None

    async def _fake_stream():
        yield chunk

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            raise Exception(
                "OpenAIException - Function tools with reasoning_effort are not supported "
                "for gpt-5.6-luna in /v1/chat/completions. To use function tools, use "
                "/v1/responses or set reasoning_effort to 'none'."
            )
        return _fake_stream()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-luna",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        tools=(ToolSpec(name="probe", description="x", parameters={"type": "object"}),),
    )
    async for _ in provider.generate(req):
        pass

    assert len(calls) == 2, "one refused attempt, then the retry"
    assert "reasoning_effort" not in calls[0]
    assert calls[1]["reasoning_effort"] == "none"


async def test_no_retry_when_the_connection_chose_a_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """params: {reasoning_effort: high} is a deliberate choice; masking the provider's
    refusal by silently downgrading it would hide the operator's own misconfiguration."""
    import litellm

    from core.ports.model_provider import ToolSpec

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        raise Exception("Function tools with reasoning_effort are not supported")

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-luna",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        tools=(ToolSpec(name="probe", description="x", parameters={"type": "object"}),),
        params={"reasoning_effort": "high"},
    )
    with pytest.raises(Exception, match="reasoning_effort"):
        async for _ in provider.generate(req):
            pass
    assert len(calls) == 1
    assert calls[0]["reasoning_effort"] == "high", "the connection's choice was sent"
