"""The inference proxy: how a coding harness in a container calls a model.

A harness brings its own agent loop and its own idea of how to reach a provider. Left to
itself it would take a provider key into the container and call the vendor directly, and
three guarantees would quietly stop holding: spend would not reach ``usage_record``, the
tenant's ``egress_policy`` would not be consulted, and daily caps would not apply -- all
three live on *our* side of ``ModelProvider``, which a direct call never crosses.

So the harness is pointed here instead. It speaks the OpenAI chat-completions dialect it
already speaks; this route re-issues the call through the same port every other caller
uses, and writes the usage row. The container therefore holds a scoped, short-lived,
revocable token instead of a provider key, and delegated spend becomes visible for the
first time -- ``adapters/mcp/codegen.py`` writes no usage row at all today.

Auth is the inference job token (``core.harness.tokens``), carried as an ordinary bearer
so a harness needs no special support: to opencode this is just an OpenAI-compatible
endpoint with an API key. There is no session JWT here, exactly as ``routes/git_http.py``
has none -- a subprocess in a container cannot do that dance.

**The requested model is advisory.** A harness names a model in its body; this route uses
the connection the *token* names. One place decides what a persona's harness may spend
on, and a harness cannot talk its way onto a more expensive model.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, StreamingResponse

from api.encryptor_factory import get_encryptor
from api.model_provider_factory import get_model_provider
from core.agents.authoring import resolve_connection_api_key
from core.agents.models import Agent, Persona
from core.audit.models import UsageRecordRow
from core.harness.tokens import InferenceGrant, verify_inference_job_token
from core.ports.model_provider import EgressDeniedError, GenerationRequest, ToolSpec
from core.tenancy.egress import load_egress_policy
from core.tenancy.generation_limits import load_generation_limits
from core.tenancy.scope import tenant_scope
from core.usage_limits import UsageLimitExceededError, ensure_within_limits

router = APIRouter(prefix="/inference", tags=["inference"])

log = structlog.get_logger()

# Every call this route makes is delegated coding work. It is not a parameter: a harness
# must not be able to bill its calls to a cheaper-looking purpose, and rule 11 keys the
# egress policy on this same taxonomy.
PURPOSE = "delegation"


class _Resolved(BaseModel):
    """What the token resolved to, server-side. None of it comes from the request body."""

    model_config = {"arbitrary_types_allowed": True}

    grant: InferenceGrant
    workspace_id: uuid.UUID | None
    provider: str
    model: str
    api_base: str | None
    api_key: str | None
    params: dict[str, Any]


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


async def _resolve(request: Request) -> _Resolved:
    """Token -> the connection it authorises. Raises 401 for anything unverifiable."""
    grant = verify_inference_job_token(_bearer(request))
    if grant is None:
        raise HTTPException(
            status_code=401,
            detail="a valid inference job token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    async with tenant_scope(grant.tenant_id) as session:
        agent = await session.get(Agent, grant.agent_id)
        persona = await session.get(Persona, grant.persona_id)
        if agent is None or agent.archived_at is not None:
            # The token was valid, but what it points at is gone or retired. 403 rather
            # than 401: re-authenticating would not help.
            raise HTTPException(
                status_code=403, detail="the connection this token names is unavailable"
            )
        resolved = _Resolved(
            grant=grant,
            workspace_id=persona.workspace_id if persona is not None else None,
            provider=agent.provider,
            model=agent.model,
            api_base=agent.api_base,
            api_key=None,
            params=dict(agent.params or {}),
        )
    key = await resolve_connection_api_key(
        grant.tenant_id, agent.credential_ref, encryptor=get_encryptor()
    )
    return resolved.model_copy(update={"api_key": key})


def _tool_specs(body: dict[str, Any]) -> tuple[ToolSpec, ...]:
    """The harness's tool declarations, in the port's shape.

    A coding harness without tools cannot read or edit a file, so this is the load-bearing
    half of the translation, not an optional extra.
    """
    specs: list[ToolSpec] = []
    for tool in body.get("tools") or []:
        function = (tool or {}).get("function") or {}
        name = function.get("name")
        if not name:
            continue
        specs.append(
            ToolSpec(
                name=str(name),
                description=str(function.get("description") or ""),
                parameters=dict(function.get("parameters") or {}),
            )
        )
    return tuple(specs)


async def _meter(
    resolved: _Resolved, prompt_tokens: int, completion_tokens: int, cached: int, latency_ms: int
) -> None:
    """One usage row per proxied call.

    Not in the same transaction as a message, because a harness call has no message to be
    atomic with -- the same honest degradation ``core/sessions/history.py`` and
    ``core/reporting/pipeline.py`` already document for summarisation and reports. What
    matters is that the row exists at all: without it this spend is invisible to
    ``core.usage_limits``, which is the state delegated work is in today.
    """
    from core.agents.runtime import _estimate_cost

    grant = resolved.grant
    cost = await _estimate_cost(resolved.provider, resolved.model, prompt_tokens, completion_tokens)
    async with tenant_scope(grant.tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=grant.tenant_id,
                workspace_id=resolved.workspace_id,
                session_id=grant.session_id,
                message_id=None,
                persona_id=grant.persona_id,
                agent_id=grant.agent_id,
                provider=resolved.provider,
                model=resolved.model,
                purpose=PURPOSE,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached,
                estimated_cost=cost,
                latency_ms=latency_ms,
            )
        )


def _chunk_payload(
    completion_id: str, model: str, delta: dict[str, Any], finish: str | None
) -> str:
    return json.dumps(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


def _completion_text(text_parts: list[str], calls: tuple[Any, ...]) -> str:
    """What the model actually produced, for token counting.

    Tool-call arguments count. Measured on the first real harness run: six proxied calls
    reported 11,680 prompt tokens and *thirteen* completion tokens, because a coding
    harness says almost nothing in prose -- its output is the arguments to read, write and
    bash. Counting only text bills a harness as if it were free, which is precisely the
    kind of blind spot this route exists to close.
    """
    parts = list(text_parts)
    for call in calls:
        parts.append(call.name)
        parts.append(json.dumps(call.arguments))
    return "".join(parts)


def _tool_call_payload(index: int, call: Any) -> dict[str, Any]:
    return {
        "index": index,
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    """OpenAI chat-completions, re-issued through ``ModelProvider``.

    Streaming and non-streaming both supported because harnesses differ; opencode streams.
    """
    resolved = await _resolve(request)
    grant = resolved.grant
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON") from None

    # Before anything is spent, and against the token's claims rather than the body's.
    try:
        await ensure_within_limits(
            grant.tenant_id, agent_id=grant.agent_id, persona_id=grant.persona_id
        )
    except UsageLimitExceededError as exc:
        # 429 so a harness can tell "you are out of budget" from "your request was wrong".
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    limits = await load_generation_limits(grant.tenant_id)
    model_string = f"{resolved.provider}/{resolved.model}"
    provider = get_model_provider(resolved.provider)
    messages = list(body.get("messages") or [])
    req = GenerationRequest(
        model=model_string,
        messages=messages,
        purpose=PURPOSE,
        tools=_tool_specs(body),
        api_base=resolved.api_base,
        api_key=resolved.api_key,
        params=resolved.params,
        egress_policy=await load_egress_policy(grant.tenant_id),
        max_generation_seconds=limits.max_seconds,
        max_generation_chars=limits.max_chars,
    )

    prompt_tokens = sum(
        provider.count_tokens(str(message.get("content") or ""), model_string)
        for message in messages
    )
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    started = time.monotonic()

    if not body.get("stream"):
        text_parts: list[str] = []
        calls: tuple[Any, ...] = ()
        cached = 0
        try:
            async for chunk in provider.generate(req):
                text_parts.append(chunk.text)
                cached = chunk.cached_tokens or cached
                if chunk.tool_calls:
                    calls = chunk.tool_calls
        except EgressDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        content = "".join(text_parts)
        completion_tokens = provider.count_tokens(_completion_text(text_parts, calls), model_string)
        await _meter(
            resolved,
            prompt_tokens,
            completion_tokens,
            cached,
            int((time.monotonic() - started) * 1000),
        )
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if calls:
            message["tool_calls"] = [_tool_call_payload(i, c) for i, c in enumerate(calls)]
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": resolved.model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if calls else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )

    def sse(delta: dict[str, Any], finish: str | None = None) -> str:
        return f"data: {_chunk_payload(completion_id, resolved.model, delta, finish)}\n\n"

    async def stream() -> AsyncIterator[str]:
        text_parts: list[str] = []
        made_calls: tuple[Any, ...] = ()
        cached = 0
        finish = "stop"
        yield sse({"role": "assistant"})
        try:
            async for chunk in provider.generate(req):
                if chunk.text:
                    text_parts.append(chunk.text)
                    yield sse({"content": chunk.text})
                cached = chunk.cached_tokens or cached
                if chunk.tool_calls:
                    finish = "tool_calls"
                    made_calls = chunk.tool_calls
                    calls = [_tool_call_payload(i, c) for i, c in enumerate(chunk.tool_calls)]
                    yield sse({"tool_calls": calls})
        except EgressDeniedError as exc:
            # Mid-stream refusal: say so in the stream, because headers are long gone.
            error = {"error": {"message": str(exc), "type": "egress_denied"}}
            yield f"data: {json.dumps(error)}\n\n"
            log.warning("inference.egress_denied", tenant_id=str(grant.tenant_id))
            return
        finally:
            await _meter(
                resolved,
                prompt_tokens,
                provider.count_tokens(_completion_text(text_parts, made_calls), model_string),
                cached,
                int((time.monotonic() - started) * 1000),
            )
        yield sse({}, finish)
        # A final usage frame, choices empty, as the OpenAI streaming API sends it.
        # Without it a harness has no idea what anything cost: opencode read zeroes off
        # this stream and reported a whole delegation as free, which is exactly the sort
        # of quiet mis-reporting this proxy exists to prevent -- and it would be ours,
        # not the provider's.
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": provider.count_tokens(
                _completion_text(text_parts, made_calls), model_string
            ),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        yield (
            "data: "
            + json.dumps(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": resolved.model,
                    "choices": [],
                    "usage": usage,
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/v1/models")
async def models(request: Request) -> Any:
    """The one model this token may use.

    Harnesses list models to validate their configuration; answering with the whole
    provider catalogue would invite one to pick something the persona is not configured
    for, which this route would then ignore anyway.
    """
    resolved = await _resolve(request)
    return {
        "object": "list",
        "data": [{"id": resolved.model, "object": "model", "owned_by": resolved.provider}],
    }
