"""Acceptance criteria for the agent runtime, against a live Postgres."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from core.agents.models import Agent
from core.agents.runtime import AllRetriesExhaustedError, run_agent_turn
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolContext, ToolRegistry, ToolResult
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ToolCall, ToolSpec
from core.process.skeleton import create_session
from core.sessions.models import MessageRow, SessionEventRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(
    slug_prefix: str, **agent_kwargs: object
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id, **agent_kwargs)  # type: ignore[arg-type]
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, sess.id, persona_id


@dataclass(frozen=True)
class _ScriptedTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    cached_tokens: int = 0
    reasoning: str = ""


@dataclass
class _ScriptedProvider:
    """A ``ModelProvider`` test double that plays back a scripted sequence of turns, one
    per call to ``generate()``, in order. ``fail_first_n`` simulates provider failures
    for retry/fallback testing."""

    turns: list[_ScriptedTurn]
    fail_first_n: int = 0
    call_count: int = field(default=0, init=False)
    # Every request's messages, so a test can see what the runtime sent back.
    requests: list[list[dict[str, object]]] = field(default_factory=list, init=False)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.call_count += 1
        self.requests.append([dict(m) for m in req.messages])
        if self.call_count <= self.fail_first_n:
            raise ConnectionError("simulated provider failure")
        turn = self.turns.pop(0)
        finish_reason = "tool_calls" if turn.tool_calls else "stop"
        yield Chunk(
            text=turn.text,
            finish_reason=finish_reason,
            tool_calls=turn.tool_calls,
            cached_tokens=turn.cached_tokens,
            reasoning=turn.reasoning,
        )

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=True, supports_json_mode=False, supports_prompt_caching=False
        )


# ── scripted turn with two tool calls: message + N usage rows + tool results, atomic ──


async def test_scripted_turn_with_two_tool_calls_commits_message_and_usage_atomically(
    db_available: None,
) -> None:
    tenant_id, session_id, persona_id = await _setup("runtime-tools")
    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="",
                tool_calls=(
                    ToolCall(id="call_1", name="roll_die", arguments={"sides": 20}),
                    ToolCall(id="call_2", name="lookup_rule", arguments={"key": "grapple"}),
                ),
            ),
            _ScriptedTurn(text="You rolled 14 and grappling succeeds."),
        ]
    )
    dispatched: list[str] = []

    async def roll_die_handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        dispatched.append("roll_die")
        return ToolResult(content="14")

    async def lookup_rule_handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        dispatched.append("lookup_rule")
        return ToolResult(content="Grapple: opposed STR check.")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="roll_die", description="Roll a die", parameters={}), roll_die_handler
    )
    registry.register(
        ToolSpec(name="lookup_rule", description="Look up a rule", parameters={}),
        lookup_rule_handler,
    )

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "I try to grapple the guard."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    assert result.content_md == "You rolled 14 and grappling succeeds."
    assert result.tool_calls_made == 2
    assert dispatched == ["roll_die", "lookup_rule"]
    assert len(result.usage_record_ids) == 2  # one per provider call (tool-call turn + final)

    async with tenant_scope(tenant_id) as session:
        messages = (
            (await session.execute(select(MessageRow).where(MessageRow.session_id == session_id)))
            .scalars()
            .all()
        )
        usage_rows = (
            (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(SessionEventRow).where(SessionEventRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(messages) == 1  # only the FINAL answer is a message row
    assert messages[0].content_md == "You rolled 14 and grappling succeeds."
    assert len(usage_rows) == 2
    assert len(events) == 1
    assert events[0].payload["tool_calls_made"] == 2


# ── provider failure -> retry -> fallback profile; no double charge ────────────────


async def test_provider_failure_retries_then_falls_back(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"runtime-fallback-{uuid.uuid4().hex[:8]}"
    )

    async with tenant_scope(tenant_id) as session:
        fallback_profile = Agent(
            tenant_id=tenant_id, name="fallback", provider="fallback-provider", model="fb-1"
        )
        session.add(fallback_profile)
        await session.flush()
        fallback_agent_id = fallback_profile.id

    persona_id = await seed_dev_agent(
        tenant_id,
        workspace_id,
        key="fallback-agent",
        provider="primary-provider",
        model="p-1",
        fallback_agent_id=fallback_agent_id,
    )
    sess = await create_session(tenant_id, workspace_id, persona_id)

    always_fails = _ScriptedProvider(turns=[], fail_first_n=999)
    succeeds = _ScriptedProvider(turns=[_ScriptedTurn(text="fallback answer")])
    providers = {"primary-provider": always_fails, "fallback-provider": succeeds}

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "hello"}],
        model_provider_factory=lambda name: providers[name],
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{uuid.uuid4()}",
        max_retries=2,
    )

    assert result.content_md == "fallback answer"
    assert always_fails.call_count == 2  # tried twice before giving up
    assert succeeds.call_count == 1

    async with tenant_scope(tenant_id) as session:
        usage_rows = (
            (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.session_id == sess.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(usage_rows) == 1  # only the fallback call is metered -- not double-charged
    assert usage_rows[0].provider == "fallback-provider"


async def test_all_profiles_exhausted_raises(db_available: None) -> None:
    tenant_id, session_id, persona_id = await _setup("runtime-exhausted")
    always_fails = _ScriptedProvider(turns=[], fail_first_n=999)

    with pytest.raises(AllRetriesExhaustedError):
        await run_agent_turn(
            tenant_id,
            persona_id,
            session_id,
            [{"role": "user", "content": "hi"}],
            model_provider_factory=lambda _name: always_fails,
            tool_registry=ToolRegistry(),
            idempotency_key=f"turn:{uuid.uuid4()}",
            max_retries=2,
        )

    async with tenant_scope(tenant_id) as session:
        messages = (
            (await session.execute(select(MessageRow).where(MessageRow.session_id == session_id)))
            .scalars()
            .all()
        )
        usage_rows = (
            (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
    assert messages == []  # nothing committed -- all or nothing
    assert usage_rows == []


# ── idempotency: retries never double-execute tools ─────────────────────────────────


async def test_retried_tool_dispatch_with_the_same_idempotency_key_does_not_reexecute(
    db_available: None,
) -> None:
    tenant_id, session_id, persona_id = await _setup("runtime-idem")
    call_count = 0

    async def counting_tool(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        nonlocal call_count
        call_count += 1
        return ToolResult(content=f"call-{call_count}")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="side_effect", description="", parameters={}), counting_tool)

    from core.agents.runtime import _dispatch_tool_idempotent

    ctx = ToolContext(tenant_id=tenant_id, persona_id=persona_id, session_id=session_id)
    tool_call = ToolCall(id="call_x", name="side_effect", arguments={})
    key = f"turn:{uuid.uuid4()}:tool:call_x"

    first = await _dispatch_tool_idempotent(
        tenant_id=tenant_id,
        idempotency_key=key,
        tool_registry=registry,
        tool_call=tool_call,
        ctx=ctx,
    )
    second = await _dispatch_tool_idempotent(
        tenant_id=tenant_id,
        idempotency_key=key,
        tool_registry=registry,
        tool_call=tool_call,
        ctx=ctx,
    )

    assert call_count == 1
    assert first == second == {"content": "call-1", "resolution_id": None}


# ── token counts in usage_record match provider-reported usage ─────────────────────


async def test_usage_record_token_counts_match_provider_reported_usage(
    db_available: None,
) -> None:
    tenant_id, session_id, persona_id = await _setup("runtime-tokens")
    provider = _ScriptedProvider(turns=[_ScriptedTurn(text="four words in this reply")])

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "three word prompt"}],
        model_provider_factory=lambda _name: provider,
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    async with tenant_scope(tenant_id) as session:
        usage_row = await session.get(UsageRecordRow, result.usage_record_ids[0])
    assert usage_row is not None
    # _ScriptedProvider.count_tokens splits on whitespace -- both sides computed the
    # exact same way the runtime itself calls count_tokens, so this proves the recorded
    # values are exactly what the provider reported, not an approximation drifting from it.
    assert usage_row.prompt_tokens == provider.count_tokens("three word prompt", "x")
    assert usage_row.completion_tokens == provider.count_tokens("four words in this reply", "x")


# ── cached_tokens threaded from the provider into usage_record ────────────────


async def test_cached_tokens_reach_the_usage_record(db_available: None) -> None:
    tenant_id, session_id, persona_id = await _setup("runtime-cached-tokens")
    provider = _ScriptedProvider(turns=[_ScriptedTurn(text="a cached reply", cached_tokens=123)])

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "hi"}],
        model_provider_factory=lambda _name: provider,
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    async with tenant_scope(tenant_id) as session:
        usage_row = await session.get(UsageRecordRow, result.usage_record_ids[0])
    assert usage_row is not None
    assert usage_row.cached_tokens == 123


async def test_persona_params_ride_over_the_connections(db_available: None) -> None:
    """A persona's own generation overrides reach the provider merged OVER its
    connection's params -- the mechanism that keeps five suspects on one shared
    connection from converging into one voice. The connection sets the base; the
    persona's temperature wins; the request's own fields still outrank both."""
    import uuid as _uuid

    from core.agents import runtime as rt
    from core.agents.authoring import create_agent, create_persona
    from core.agents.tools import ToolRegistry
    from core.tenancy.seed import seed_dev_tenant

    tenant_id, _o, workspace_id = await seed_dev_tenant(slug=f"pp-{_uuid.uuid4().hex[:8]}")
    from adapters.encryptor.identity import IdentityEncryptor

    profile = await create_agent(
        tenant_id,
        "conn",
        "echo",
        "echo-model",
        params={"temperature": 0.2, "top_p": 0.9},
        encryptor=IdentityEncryptor(),
    )
    persona = await create_persona(
        tenant_id,
        workspace_id,
        "spread",
        "Spread",
        profile.id,
        params={"temperature": 0.9, "presence_penalty": 0.4},
    )

    seen: list[dict] = []

    class _Provider:
        async def generate(self, req):  # noqa: ANN001, ANN202
            seen.append(dict(req.params))
            from core.ports.model_provider import Chunk

            yield Chunk(text="ok", finish_reason="stop")

        def count_tokens(self, text, model):  # noqa: ANN001, ANN202
            return 1

        def capabilities(self, model):  # noqa: ANN001, ANN202
            from core.ports.model_provider import Capabilities

            return Capabilities(
                supports_tools=False, supports_json_mode=False, supports_prompt_caching=False
            )

    from core.process.skeleton import create_session

    sess = await create_session(tenant_id, workspace_id, persona.id)
    await rt.run_agent_turn(
        tenant_id,
        persona.id,
        sess.id,
        [{"role": "user", "content": "hi"}],
        model_provider_factory=lambda _p: _Provider(),
        tool_registry=ToolRegistry(),
        idempotency_key=f"t:{sess.id}:0",
        encryptor=IdentityEncryptor(),
        event_seq=0,
    )

    assert seen, "the provider was never called"
    merged = seen[0]
    assert merged["temperature"] == 0.9, "persona wins over connection"
    assert merged["top_p"] == 0.9, "connection fills what the persona left alone"
    assert merged["presence_penalty"] == 0.4


async def test_a_model_that_never_stops_calling_tools_is_made_to_answer(
    db_available: None,
) -> None:
    """Exhausting the tool loop used to raise, which paused the whole session and threw
    away the turn's real work -- a newsroom desk that had searched six times took the
    session down with it. A model still reaching for tools has failed to STOP, not
    failed, so the tools are taken away and it is asked once more."""
    tenant_id, session_id, persona_id = await _setup("runtime-toolloop")
    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="", tool_calls=(ToolCall(id=f"c{i}", name="search", arguments={"q": "x"}),)
            )
            for i in range(3)
        ]
        + [_ScriptedTurn(text="Filed from what I already have.")]
    )

    async def search_handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        return ToolResult(content="a result")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="search", description="Search", parameters={}), search_handler)

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "file a story"}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
        max_tool_loop=3,
    )

    assert result.content_md == "Filed from what I already have."
    assert result.tool_calls_made == 3


async def test_a_thinking_models_reasoning_goes_back_with_its_tool_call(
    db_available: None,
) -> None:
    """DeepSeek in thinking mode refuses the follow-up request unless the assistant
    message carrying the tool_calls also carries the reasoning behind them. It is sent
    only when the provider produced one: an OpenAI-shaped endpoint gets the message it
    always got."""
    tenant_id, session_id, persona_id = await _setup("runtime-reasoning")
    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="",
                tool_calls=(ToolCall(id="call_1", name="roll_die", arguments={"sides": 20}),),
                reasoning="A reaction check is 2d6; call the randomizer.",
            ),
            _ScriptedTurn(text="The result is 7."),
        ]
    )

    async def roll_die_handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        return ToolResult(content="7")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="roll_die", description="Roll a die", parameters={}), roll_die_handler
    )
    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "The reeve eyes the strangers."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
    )
    assert result.content_md == "The result is 7."

    follow_up = provider.requests[1]
    assistant = next(m for m in follow_up if m.get("role") == "assistant" and m.get("tool_calls"))
    assert assistant["reasoning_content"] == "A reaction check is 2d6; call the randomizer."


async def test_no_reasoning_means_no_reasoning_field(db_available: None) -> None:
    tenant_id, session_id, persona_id = await _setup("runtime-no-reasoning")
    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="", tool_calls=(ToolCall(id="call_1", name="roll_die", arguments={}),)
            ),
            _ScriptedTurn(text="Done."),
        ]
    )

    async def roll_die_handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        return ToolResult(content="7")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="roll_die", description="Roll a die", parameters={}), roll_die_handler
    )
    await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "Roll."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
    )
    assistant = next(
        m for m in provider.requests[1] if m.get("role") == "assistant" and m.get("tool_calls")
    )
    assert "reasoning_content" not in assistant
