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


async def test_empty_generation_retries_once_with_reasoning_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reasoning model can burn its whole completion budget on hidden reasoning and
    stream nothing (seen live: gpt-5.6-terra mid-interrogation). With no budget set and
    no effort chosen, the only lever is forcing reasoning off for one retry."""
    import litellm

    calls: list[dict] = []

    def _stream(chunks):  # noqa: ANN001, ANN202
        async def gen():
            for c in chunks:
                yield c

        return gen()

    def _delta(content, finish=None):  # noqa: ANN001, ANN202
        part = MagicMock()
        choice = MagicMock()
        choice.delta = MagicMock(content=content, tool_calls=None)
        choice.finish_reason = finish
        part.choices = [choice]
        part.usage = None
        return part

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            return _stream([_delta("", "stop")])  # empty: all budget went to reasoning
        return _stream([_delta("There you are."), _delta("", "stop")])

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "answer in character"}],
        purpose="generation",
    )
    text = "".join([c.text async for c in provider.generate(req)])

    assert text == "There you are."
    assert len(calls) == 2
    assert "reasoning_effort" not in calls[0]
    assert calls[1]["reasoning_effort"] == "none"


async def test_a_generation_that_only_ever_thinks_is_cut_off_on_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The character ceiling cannot catch this one. A reasoning model's thinking arrives
    as `reasoning_content`, never as content, so a turn that thinks forever streams
    chunks endlessly while emitting nothing to count. Observed live at 22, 58 and 79
    minutes with the character guard in place and unable to help."""
    import litellm

    def _thinking_delta():  # noqa: ANN202
        part = MagicMock()
        choice = MagicMock()
        # content is empty forever; only the hidden reasoning field advances
        choice.delta = MagicMock(content="", tool_calls=None)
        choice.finish_reason = None
        part.choices = [choice]
        part.usage = None
        return part

    async def _acompletion(**_kwargs):  # noqa: ANN003, ANN202
        async def gen():
            for _ in range(1_000_000):
                yield _thinking_delta()

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="ollama_chat/qwen3:30b-a3b",
        messages=[{"role": "user", "content": "file your story"}],
        purpose="generation",
        max_generation_seconds=0.05,
    )

    chunks = [c async for c in provider.generate(req)]

    # It returns at all, which is the whole point -- unguarded this never came back.
    assert "".join(c.text for c in chunks).strip() == ""


async def test_a_generation_that_will_not_stop_is_cut_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed live: 470,244 characters over 79 minutes, which then became the next
    turn's prompt and ran away again. num_ctx bounds what a model SEES, not what it may
    emit, and the job heartbeat keeps a runaway alive because a lease measures silence."""
    import litellm

    def _delta(content, finish=None):  # noqa: ANN001, ANN202
        part = MagicMock()
        choice = MagicMock()
        choice.delta = MagicMock(content=content, tool_calls=None)
        choice.finish_reason = finish
        part.choices = [choice]
        part.usage = None
        return part

    async def _acompletion(**_kwargs):  # noqa: ANN003, ANN202
        async def gen():
            for _ in range(1000):  # a model that never emits a finish_reason
                yield _delta("x" * 100)

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="ollama_chat/qwen3:30b-a3b",
        messages=[{"role": "user", "content": "file your story"}],
        purpose="generation",
        max_generation_chars=500,
    )
    chunks = [c async for c in provider.generate(req)]
    text = "".join(c.text for c in chunks)

    assert len(text) <= 700, f"runaway not cut off: {len(text)} chars"
    assert chunks[-1].finish_reason == "length"


async def test_whitespace_only_generation_counts_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed live: qwen3:30b-a3b streamed thirty newlines and nothing else. Treated as
    output, the retry below is skipped and the runtime rejects the same text as empty --
    then retries the IDENTICAL request, which only reproduces it. Whitespace is not an
    answer."""
    import litellm

    calls: list[dict] = []

    def _stream(chunks):  # noqa: ANN001, ANN202
        async def gen():
            for c in chunks:
                yield c

        return gen()

    def _delta(content, finish=None):  # noqa: ANN001, ANN202
        part = MagicMock()
        choice = MagicMock()
        choice.delta = MagicMock(content=content, tool_calls=None)
        choice.finish_reason = finish
        part.choices = [choice]
        part.usage = None
        return part

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            return _stream([_delta("\n" * 30), _delta("", "stop")])
        return _stream([_delta("The meeting is open."), _delta("", "stop")])

    monkeypatch.setattr(litellm, "acompletion", _acompletion)

    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="ollama_chat/qwen3:30b-a3b",
        messages=[{"role": "user", "content": "open the news meeting"}],
        purpose="generation",
    )
    text = "".join([c.text async for c in provider.generate(req)])

    assert "The meeting is open." in text
    assert len(calls) == 2
    assert calls[1]["reasoning_effort"] == "none"


async def test_a_nonempty_generation_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)

        async def gen():
            part = MagicMock()
            choice = MagicMock()
            choice.delta = MagicMock(content="hello", tool_calls=None)
            choice.finish_reason = "stop"
            part.choices = [choice]
            part.usage = None
            yield part

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
    )
    text = "".join([c.text async for c in provider.generate(req)])
    assert text == "hello"
    assert len(calls) == 1, "a productive generation must not retry"


async def test_empty_generation_with_a_budget_retries_bigger_and_keeps_the_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cause is budget starvation, not reasoning itself. A deliberate high-effort
    run must STAY high-effort on the retry -- silently downgrading it would corrupt
    exactly the experiment that set it -- so the retry raises max_tokens instead and
    leaves reasoning_effort untouched, at every level."""
    import litellm

    calls: list[dict] = []

    def _delta(content, finish=None):  # noqa: ANN001, ANN202
        part = MagicMock()
        choice = MagicMock()
        choice.delta = MagicMock(content=content, tool_calls=None)
        choice.finish_reason = finish
        part.choices = [choice]
        part.usage = None
        return part

    def _stream(chunks):  # noqa: ANN001, ANN202
        async def gen():
            for c in chunks:
                yield c

        return gen()

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            return _stream([_delta("", "stop")])
        return _stream([_delta("After long thought: Elin."), _delta("", "stop")])

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "who did it?"}],
        purpose="generation",
        max_tokens=900,
        params={"reasoning_effort": "high"},
    )
    text = "".join([c.text async for c in provider.generate(req)])

    assert text == "After long thought: Elin."
    assert len(calls) == 2
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[1]["reasoning_effort"] == "high", "the chosen effort survives the retry"
    assert calls[1]["max_tokens"] == max(900 * 4, 2048), "the budget is what got raised"


async def test_params_supplied_budget_also_gets_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_tokens set on the connection/persona params (not the request) is still a
    budget the retry can raise -- reading req.max_tokens alone would have missed it and
    wrongly fallen through to the effort-off path."""
    import litellm

    calls: list[dict] = []

    def _delta(content, finish=None):  # noqa: ANN001, ANN202
        part = MagicMock()
        choice = MagicMock()
        choice.delta = MagicMock(content=content, tool_calls=None)
        choice.finish_reason = finish
        part.choices = [choice]
        part.usage = None
        return part

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)

        async def gen():
            if len(calls) == 1:
                yield _delta("", "stop")
            else:
                yield _delta("ok", "stop")

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        params={"max_tokens": 500},
    )
    text = "".join([c.text async for c in provider.generate(req)])
    assert text == "ok"
    assert calls[1]["max_tokens"] == 2048
    assert "reasoning_effort" not in calls[1]


async def test_chosen_effort_with_no_budget_is_not_papered_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No budget to raise AND an effort the caller chose: there is no honest retry.
    The empty generation surfaces to the runtime instead of the adapter silently
    downgrading a deliberate configuration."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)

        async def gen():
            part = MagicMock()
            choice = MagicMock()
            choice.delta = MagicMock(content="", tool_calls=None)
            choice.finish_reason = "stop"
            part.choices = [choice]
            part.usage = None
            yield part

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        params={"reasoning_effort": "high"},
    )
    text = "".join([c.text async for c in provider.generate(req)])
    assert text == ""
    assert len(calls) == 1, "no silent downgrade of a deliberate effort"


async def test_a_parameter_the_endpoint_refuses_is_dropped_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection and persona params exist so ONE connection can serve models with
    different knobs -- and models genuinely differ: gpt-5.6-terra refuses a
    presence_penalty other OpenAI models accept, which killed whole RPG turns. Only the
    keys the endpoint named are dropped, and everything else survives the retry."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            raise Exception(  # noqa: TRY002
                "litellm.UnsupportedParamsError: openai does not support parameters: "
                "['presence_penalty'], for model=gpt-5.6-terra."
            )

        async def gen():
            part = MagicMock()
            choice = MagicMock()
            choice.delta = MagicMock(content="in character", tool_calls=None)
            choice.finish_reason = "stop"
            part.choices = [choice]
            part.usage = None
            yield part

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "speak"}],
        purpose="generation",
        params={"presence_penalty": 0.4, "temperature": 0.9},
    )
    text = "".join([c.text async for c in provider.generate(req)])

    assert text == "in character"
    assert len(calls) == 2
    assert "presence_penalty" not in calls[1], "the refused parameter was not dropped"
    assert calls[1]["temperature"] == 0.9, "an unrelated parameter must survive the retry"


async def test_an_unrelated_failure_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping parameters is only correct when the endpoint named them. Anything else is
    a real failure the runtime must see."""
    import litellm

    async def _acompletion(**_kwargs):  # noqa: ANN003, ANN202
        raise Exception("litellm.AuthenticationError: invalid api key")  # noqa: TRY002

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
    )
    with pytest.raises(Exception, match="AuthenticationError"):
        [c async for c in provider.generate(req)]


async def test_the_reasoning_retry_does_not_require_locally_declared_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint is the authority on what it was sent. A workspace's remote tools can
    reach a request without the phase declaring any, so gating this retry on a local
    ``tools`` variable made it stop firing for exactly the turns that needed it -- seen
    live as an RPG session that paused every time the players took a turn."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if len(calls) == 1:
            raise Exception(  # noqa: TRY002
                "litellm.BadRequestError: Function tools with reasoning_effort are not "
                "supported for gpt-5.6-terra. Set reasoning_effort to 'none'."
            )

        async def gen():
            part = MagicMock()
            choice = MagicMock()
            choice.delta = MagicMock(content="Bram lifts the lantern.", tool_calls=None)
            choice.finish_reason = "stop"
            part.choices = [choice]
            part.usage = None
            yield part

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "your move"}],
        purpose="generation",
        # No tools on the request at all -- the refusal still arrives.
    )
    text = "".join([c.text async for c in provider.generate(req)])

    assert text == "Bram lifts the lantern."
    assert len(calls) == 2
    assert calls[1]["reasoning_effort"] == "none"


async def test_repairs_chain_when_one_refusal_hides_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real request carries several knobs and an endpoint refuses them one at a time:
    the unsupported sampling parameter first, then -- on the repaired call -- the
    reasoning effort. Fixing one inside the other's handler left the second refusal
    uncaught, which paused a live RPG session on every player turn."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if "presence_penalty" in kwargs:
            raise Exception(  # noqa: TRY002
                "litellm.UnsupportedParamsError: openai does not support parameters: "
                "['presence_penalty'], for model=gpt-5.6-terra."
            )
        if kwargs.get("reasoning_effort") != "none":
            raise Exception(  # noqa: TRY002
                "litellm.BadRequestError: Function tools with reasoning_effort are not "
                "supported for gpt-5.6-terra. Set reasoning_effort to 'none'."
            )

        async def gen():
            part = MagicMock()
            choice = MagicMock()
            choice.delta = MagicMock(content="Pip listens at the door.", tool_calls=None)
            choice.finish_reason = "stop"
            part.choices = [choice]
            part.usage = None
            yield part

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "your move"}],
        purpose="generation",
        params={"presence_penalty": 0.4, "temperature": 0.9},
    )
    text = "".join([c.text async for c in provider.generate(req)])

    assert text == "Pip listens at the door."
    assert len(calls) == 3, "both refusals must be repaired, in turn"
    assert "presence_penalty" not in calls[-1]
    assert calls[-1]["reasoning_effort"] == "none"
    assert calls[-1]["temperature"] == 0.9, "unrelated parameters survive every repair"


async def test_an_unrepairable_refusal_does_not_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chained repairs must terminate: a refusal nothing can fix has to raise, not spin."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        raise Exception("litellm.RateLimitError: slow down")  # noqa: TRY002

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-terra",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
    )
    with pytest.raises(Exception, match="RateLimitError"):
        [c async for c in provider.generate(req)]
    assert len(calls) == 1


async def test_a_connection_overrides_the_platform_default_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """num_ctx is a property of the tenant's model and hardware, so a connection that
    sets it must win over the platform default. It used to do neither: the streaming path
    splatted both into one dict and raised `got multiple values for keyword argument`
    every turn, and the structured path accepted the value and silently ignored it."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)

        async def gen():
            part = MagicMock()
            choice = MagicMock()
            choice.delta = MagicMock(content="ok", tool_calls=None)
            choice.finish_reason = "stop"
            part.choices = [choice]
            part.usage = None
            yield part

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()

    big = GenerationRequest(
        model="ollama/qwen3.8:27b",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
        params={"num_ctx": 32768},
    )
    text = "".join([c.text async for c in provider.generate(big)])
    assert text == "ok"
    assert calls[0]["num_ctx"] == 32768, "the connection's window must win"

    # And a connection with no opinion still gets the default that keeps Ollama from
    # silently returning nothing.
    calls.clear()
    plain = GenerationRequest(
        model="ollama/qwen3.8:27b",
        messages=[{"role": "user", "content": "hi"}],
        purpose="generation",
    )
    [c async for c in provider.generate(plain)]
    assert calls[0]["num_ctx"] == 16384


async def test_a_refused_parameter_value_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the repair matched only "does not support parameters: [...]". Reasoning
    models refuse a *value* instead -- they accept `temperature`, but only at their default
    -- and that wording matched nothing, so the call failed outright. In delegation that
    surfaced as every coding agent silently shipping its placeholder scaffold, because
    codegen sends temperature=0.2 and treats a failed generation as "no file blocks"."""
    import litellm

    calls: list[dict] = []

    async def _acompletion(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        if "temperature" in kwargs:
            raise Exception(  # noqa: TRY002
                "litellm.BadRequestError: OpenAIException - Unsupported value: "
                "'temperature' does not support 0.2 with this model. Only the default (1) "
                "value is supported."
            )

        async def gen():  # noqa: ANN202
            part = MagicMock()
            choice = MagicMock()
            choice.delta = MagicMock(content="### FILE: README.md", tool_calls=None)
            choice.finish_reason = "stop"
            part.choices = [choice]
            part.usage = None
            yield part

        return gen()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    provider = LiteLLMModelProvider()
    req = GenerationRequest(
        model="openai/gpt-5.6-luna",
        messages=[{"role": "user", "content": "write the readme"}],
        purpose="delegation",
        temperature=0.2,
        params={"max_tokens": 4000},
    )
    text = "".join([c.text async for c in provider.generate(req)])

    assert text == "### FILE: README.md"
    assert len(calls) == 2
    assert "temperature" not in calls[1], "the refused value's parameter was not dropped"
    assert calls[1]["max_tokens"] == 4000, "an unrelated parameter must survive the retry"


def test_a_value_refusal_only_drops_the_parameter_it_names() -> None:
    """The same honesty rule the parameter-refusal repair follows: never drop a key the
    endpoint did not name, and never act on a message that named nothing."""
    from adapters.models.litellm.provider import _unsupported_value_param

    named = _unsupported_value_param(
        Exception(
            "Unsupported value: 'temperature' does not support 0.2 with this model. "
            "Only the default (1) value is supported."
        )
    )
    assert named == "temperature"
    assert _unsupported_value_param(Exception("AuthenticationError: invalid api key")) is None
