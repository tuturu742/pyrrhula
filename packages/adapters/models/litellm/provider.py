"""v1 ModelProvider: wraps LiteLLM. Ollama is a first-class provider reached through
LiteLLM's ``ollama/`` model prefix, not a separate adapter — there is nothing LiteLLM-
specific to duplicate in packages/adapters/models/ollama/.

``litellm`` and ``tiktoken`` are imported lazily inside the methods that need them, so
importing this module (and running the egress-denied path, which never reaches them)
doesn't pay their import cost and doesn't require them to be configured with any
provider credentials.

**Tool-call streaming accumulation (B1.7) is implemented against the standard OpenAI-
compatible streaming delta format LiteLLM normalises every provider to** -- each
``delta.tool_calls`` fragment carries an ``index`` grouping it with earlier fragments for
the same logical call; ``id``/``function.name`` arrive once (the first fragment for that
index), ``function.arguments`` arrives incrementally as JSON-string fragments to be
concatenated and parsed only once complete. This is a widely-implemented, stable format
(LiteLLM's entire purpose is normalising to it), but this specific code path has **not**
been exercised against a live provider in this environment (no Ollama/API key available
here) -- flagged honestly rather than silently assumed correct; ``EchoModelProvider``-style
test doubles are what actually exercise the agent runtime's own test suite.

**Prompt-cache boundary marking (C1.4)** follows Anthropic's ``cache_control`` convention,
which LiteLLM passes through unmodified for Anthropic-family models: the message at
``req.cache_boundary_index`` gets its ``content`` rewritten from a plain string to a
single-block list with ``cache_control: {"type": "ephemeral"}`` on that block, marking
"cache everything up to and including here." Applied only when
``capabilities(req.model).supports_prompt_caching`` is true; providers that don't
recognise the field ignore it (LiteLLM strips/no-ops unsupported params per-provider).
**Also not exercised against a live caching provider in this environment** -- same honest
flag as the tool-call accumulation above. ``cached_tokens`` extraction from a streamed
response's terminal ``usage`` is similarly best-effort: not every provider (or every
streaming configuration) reports it, and the attribute name itself differs
(``prompt_tokens_details.cached_tokens`` OpenAI-style vs ``cache_read_input_tokens``
Anthropic-style) -- both are checked, defaulting to 0 if neither is present.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any, TypeVar

from pydantic import BaseModel

from core.ports.model_provider import (
    Capabilities,
    Chunk,
    GenerationRequest,
    ToolCall,
    check_egress,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

_PROMPT_CACHING_MARKERS = ("claude", "gpt-4", "gpt-5", "gemini-1.5", "gemini-2")


def _tool_specs_to_litellm(req: GenerationRequest) -> list[dict[str, Any]] | None:
    if not req.tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in req.tools
    ]


def _apply_cache_boundary(
    messages: list[dict[str, object]], boundary_index: int | None, *, supports_caching: bool
) -> list[dict[str, Any]]:
    if boundary_index is None or not supports_caching or not (0 <= boundary_index < len(messages)):
        return list(messages)
    marked: list[dict[str, Any]] = list(messages)
    boundary_message = dict(marked[boundary_index])
    boundary_message["content"] = [
        {
            "type": "text",
            "text": boundary_message["content"],
            "cache_control": {"type": "ephemeral"},
        }
    ]
    marked[boundary_index] = boundary_message
    return marked


def _extract_cached_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is not None:
            return int(cached)
    cache_read = getattr(usage, "cache_read_input_tokens", None)
    return int(cache_read) if cache_read is not None else 0


def _rejects_schema_constrained_output(exc: Exception) -> bool:
    """Does this failure mean the endpoint cannot do schema-constrained JSON?

    OpenAI-compatible endpoints differ on `response_format`: DeepSeek answers "This
    response_format type is unavailable now" for `json_schema` while accepting
    `json_object`. Match on the parameter rather than one vendor's sentence, so the next
    provider with the same gap does not need its own special case.
    """
    text = str(exc).lower()
    return "response_format" in text or "json_schema" in text


def _schema_in_the_prompt(
    messages: list[dict[str, Any]], schema: type[BaseModel]
) -> list[dict[str, Any]]:
    """The fallback when the endpoint will not enforce a schema: ask for it in words."""
    return [
        *messages,
        {
            "role": "user",
            "content": (
                "Reply with ONLY a JSON object conforming to this JSON Schema. "
                "No prose, no code fences.\n" + json.dumps(schema.model_json_schema())
            ),
        },
    ]


def _first_json_object(raw: str) -> str:
    """The first complete JSON object in a reply, tolerating fences and stray prose.

    Unconstrained output is not guaranteed to be bare JSON, so this does what the
    `response_format` the endpoint refused would have done for us.
    """
    text = (raw or "").strip()
    if "```" in text:
        fenced = text.split("```")
        if len(fenced) >= 3:
            text = fenced[1].removeprefix("json").strip()
    start = text.find("{")
    if start == -1:
        return text
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return text
    return json.dumps(obj)


def _parse_streamed_arguments(raw: str) -> dict[str, Any]:
    """Tolerant tool-argument parse. Some local models (observed: qwen3 via ollama_chat)
    stream the SAME arguments object more than once, so the accumulated fragments read
    '{}{"q": ...}' -- strict json.loads dies with 'Extra data'. Decode every complete
    object and keep the last non-empty dict; garbage degrades to {} (the tool handler's
    own validation reports missing args to the model) instead of killing the turn."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        objs: list[Any] = []
        idx = 0
        while idx < len(raw):
            try:
                obj, idx = decoder.raw_decode(raw, idx)
            except json.JSONDecodeError:
                break
            objs.append(obj)
            while idx < len(raw) and raw[idx] in " \t\r\n,":
                idx += 1
        for obj in reversed(objs):
            if isinstance(obj, dict) and obj:
                return obj
        return {}


def _anthropic_turn_shim(model: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic requires the conversation to END with a user message (a trailing
    assistant message reads as prefill, which claude-opus-5 rejects outright). In a
    multi-agent session every prior persona turn is assistant-role, so the tail is
    routinely assistant. Append a neutral user nudge for anthropic/ models only --
    other providers keep byte-identical prompts."""
    if model.startswith("anthropic/") and messages and messages[-1].get("role") == "assistant":
        return [*messages, {"role": "user", "content": "(It is now your turn to respond.)"}]
    return messages


def _dispatch_model(model: str, has_tools: bool) -> str:
    """LiteLLM-internal endpoint choice (nothing outside this adapter knows LiteLLM has
    two Ollama routes): `ollama/` speaks /api/generate, which cannot do native tool
    calls -- LiteLLM emulates them by asking for JSON in the prompt, and the "call" comes
    back as plain text the runtime never dispatches. `ollama_chat/` speaks /api/chat,
    where tool-capable models (qwen2.5, llama3.x, hermes3) emit real tool_calls. Route
    tool-bearing requests there; keep plain generation on the default path unchanged."""
    if has_tools and model.startswith("ollama/"):
        return "ollama_chat/" + model.removeprefix("ollama/")
    return model


# Keys the platform manages itself; a connection's params may not override them.
# api_key/api_base come from the sealed credential and the profile's own field, tools
# and response_format from the request -- letting params replace any of these would turn
# a convenience knob into a way to reroute calls.
_MANAGED_KEYS = frozenset(
    {"model", "messages", "tools", "api_key", "api_base", "stream", "response_format", "n"}
)


def _merge_connection_params(kwargs: dict[str, Any], params: Any) -> dict[str, Any]:
    """Connection passthrough knobs, under the platform's values.

    Managed keys are dropped; for everything else an explicit request value wins and a
    params entry fills the gaps (None counts as unset -- temperature is passed as None
    when the request has no opinion, and a connection default should be able to supply
    one).
    """
    for key, value in dict(params or {}).items():
        if key in _MANAGED_KEYS:
            continue
        if kwargs.get(key) is None:
            kwargs[key] = value
    return kwargs


def _rejects_tools_with_reasoning(exc: Exception) -> bool:
    """Some chat-completions endpoints (observed: gpt-5.6-luna) refuse function tools
    while a reasoning_effort is in play and say to set it to 'none'. Matched on the
    parameter names, not one vendor's sentence."""
    text = str(exc).lower()
    return "reasoning_effort" in text and "tool" in text


class LiteLLMModelProvider:
    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        check_egress(req.purpose, req.model, req.egress_policy)

        import litellm

        # Cross-provider compatibility shims (e.g. Anthropic rejects a request whose
        # messages are all system-role -- the first turn of a session is exactly that;
        # modify_params inserts the dummy user message LiteLLM documents for this).
        litellm.modify_params = True

        supports_caching = self.capabilities(req.model).supports_prompt_caching
        messages = _apply_cache_boundary(
            req.messages, req.cache_boundary_index, supports_caching=supports_caching
        )

        tools = _tool_specs_to_litellm(req)
        extra: dict[str, Any] = {}
        if req.model.startswith(("ollama/", "ollama_chat/")):
            # Ollama's default context window is 4096 tokens; a real prompt (assembled
            # manifest, repo files for codegen) plus the requested max_tokens easily
            # exceeds it, and Ollama then silently returns EMPTY generations. num_ctx is
            # the per-request window; deployment-tunable, generous default.
            extra["num_ctx"] = int(os.environ.get("PYRRHULA_OLLAMA_NUM_CTX", "16384"))
        messages = _anthropic_turn_shim(req.model, messages)
        kwargs: dict[str, Any] = _merge_connection_params(
            {
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
            },
            req.params,
        )
        call = dict(
            model=_dispatch_model(req.model, bool(tools)),
            messages=messages,
            tools=tools,
            api_base=req.api_base,
            api_key=req.api_key,
            stream=True,
            stream_options={"include_usage": True},
            **extra,
            **kwargs,
        )
        try:
            response = await litellm.acompletion(**call)
        except Exception as exc:
            # Endpoints that refuse function tools while a reasoning effort is in play
            # (observed: gpt-5.6-luna) tell the caller to set it to 'none'. Do that once,
            # unless the connection's params already chose a value -- then the choice is
            # deliberate and the error is the operator's to see.
            if (
                not tools
                or not _rejects_tools_with_reasoning(exc)
                or "reasoning_effort" in dict(req.params or {})
            ):
                raise
            response = await litellm.acompletion(**{**call, "reasoning_effort": "none"})
        # Accumulates streamed tool-call argument fragments by index -- see module
        # docstring. Never yielded mid-accumulation: only once, on the chunk carrying
        # finish_reason == 'tool_calls'.
        pending_calls: dict[int, dict[str, Any]] = {}

        async for part in response:
            cached_tokens = _extract_cached_tokens(getattr(part, "usage", None))
            if not part.choices:
                # The terminal usage-only chunk (stream_options include_usage) some
                # providers send after the last real delta -- nothing to accumulate,
                # but its usage was already captured above for the *next* iteration's
                # cached_tokens if this loop had one more delta; for providers that put
                # usage on the same chunk as the last delta instead, cached_tokens below
                # already picks it up in that same iteration.
                continue
            delta = part.choices[0].delta
            finish_reason = part.choices[0].finish_reason
            text = delta.content or ""

            for fragment in delta.tool_calls or []:
                slot = pending_calls.setdefault(
                    fragment.index, {"id": None, "name": None, "arguments": ""}
                )
                if fragment.id:
                    slot["id"] = fragment.id
                if fragment.function and fragment.function.name:
                    slot["name"] = fragment.function.name
                if fragment.function and fragment.function.arguments:
                    slot["arguments"] += fragment.function.arguments

            # Flush accumulated calls on ANY terminal chunk that has some, not only on
            # finish_reason == 'tool_calls': LiteLLM's ollama_chat route streams real
            # tool-call deltas but finishes with plain 'stop', and holding the flush
            # hostage to the OpenAI-style reason silently dropped every local-model tool
            # call (empty message, zero dispatches).
            if finish_reason and pending_calls:
                tool_calls = tuple(
                    ToolCall(
                        id=slot["id"] or f"call_{index}",
                        name=slot["name"] or "",
                        arguments=_parse_streamed_arguments(slot["arguments"]),
                    )
                    for index, slot in sorted(pending_calls.items())
                )
                yield Chunk(
                    text=text,
                    finish_reason="tool_calls",
                    tool_calls=tool_calls,
                    cached_tokens=cached_tokens,
                )
            elif text or finish_reason:
                yield Chunk(text=text, finish_reason=finish_reason, cached_tokens=cached_tokens)

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        check_egress(req.purpose, req.model, req.egress_policy)

        import litellm

        # Cross-provider compatibility shims (e.g. Anthropic rejects a request whose
        # messages are all system-role -- the first turn of a session is exactly that;
        # modify_params inserts the dummy user message LiteLLM documents for this).
        litellm.modify_params = True

        # Structured output must ride /api/chat: on the `ollama/` (/api/generate) route,
        # reasoning models (qwen3.*) return EMPTY content for response_format requests --
        # the same family of route mismatch _dispatch_model handles for tools. `ollama_chat/`
        # returns the JSON in content (thinking lands in reasoning_content, discarded here).
        model = req.model
        if model.startswith("ollama/"):
            model = "ollama_chat/" + model.removeprefix("ollama/")
        extra: dict[str, Any] = {}
        if model.startswith("ollama_chat/"):
            # Same silent-empty-generation hazard as generate(): default num_ctx is 4096.
            extra["num_ctx"] = int(os.environ.get("PYRRHULA_OLLAMA_NUM_CTX", "16384"))
        messages = _anthropic_turn_shim(model, list(req.messages))
        common: dict[str, Any] = _merge_connection_params(
            {
                "model": model,
                "api_base": req.api_base,
                "api_key": req.api_key,
                **extra,
            },
            req.params,
        )
        try:
            response = await litellm.acompletion(
                messages=messages, response_format=schema, **common
            )
        except Exception as exc:
            if not _rejects_schema_constrained_output(exc):
                raise
            # The endpoint does schema-constrained output no favours (DeepSeek rejects
            # `json_schema` outright while accepting `json_object`). Ask for the same
            # object with the schema in the prompt and validate it here -- the port's
            # contract is a validated instance, not a particular wire parameter. Without
            # this, every structured call on such a provider failed: persona drafting,
            # knowledge drafting, anything using generate_structured.
            response = await litellm.acompletion(
                messages=_schema_in_the_prompt(messages, schema),
                response_format={"type": "json_object"},
                **common,
            )
        content = response.choices[0].message.content
        return schema.model_validate_json(_first_json_object(content))

    def count_tokens(self, text: str, model: str) -> int:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            # Approximate for non-OpenAI models (Ollama/Anthropic/Gemini): tokenizer
            # parity across providers is a real problem (plan §13.1) but cl100k_base is
            # a reasonable v1 budget-planning approximation, not a billing source of truth.
            encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))

    def capabilities(self, model: str) -> Capabilities:
        lowered = model.lower()
        return Capabilities(
            supports_tools=True,
            supports_json_mode=True,
            supports_prompt_caching=any(marker in lowered for marker in _PROMPT_CACHING_MARKERS),
        )
