"""Tool calls a model gets wrong are answered, not crashed on; what a model says alongside
its tool calls stays in the transcript; model calls made for a turn outside its loop are
metered with it."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from core.agents.runtime import UsagePoint, merge_turn_text, run_agent_turn
from core.agents.tests.test_runtime import _ScriptedProvider, _ScriptedTurn, _setup
from core.agents.tools import ToolContext, ToolRegistry, ToolResult, argument_errors
from core.audit.models import UsageRecordRow
from core.ports.model_provider import ToolCall, ToolSpec
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope

_CTX = ToolContext(tenant_id=uuid.uuid4(), persona_id=uuid.uuid4(), session_id=None)
_CLAIM = {
    "type": "object",
    "properties": {"seat": {"type": "integer"}, "item": {"type": "string"}},
    "required": ["seat", "item"],
    "additionalProperties": False,
}


def _registry(schema: dict[str, object], seen: list[dict[str, object]]) -> ToolRegistry:
    async def handler(args: dict[str, object], _ctx: ToolContext) -> ToolResult:
        seen.append(args)
        return ToolResult(content="ok")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="claim", description="claim", parameters=schema), handler)
    return registry


async def test_an_unknown_tool_is_an_error_result_not_an_exception() -> None:
    result = await _registry(_CLAIM, []).dispatch(ToolCall(id="1", name="clam", arguments={}), _CTX)
    body = json.loads(result.content)
    assert body["error"] == "unknown_tool"
    assert body["available"] == ["claim"]


async def test_invalid_arguments_are_answered_and_the_handler_never_runs() -> None:
    seen: list[dict[str, object]] = []
    result = await _registry(_CLAIM, seen).dispatch(
        ToolCall(id="1", name="claim", arguments={"seat": "two", "extra": 1}), _CTX
    )
    body = json.loads(result.content)
    assert body["error"] == "invalid_arguments"
    assert any("seat" in d for d in body["details"])
    assert any("item" in d for d in body["details"])  # the missing required field too
    assert seen == []


async def test_valid_arguments_reach_the_handler() -> None:
    seen: list[dict[str, object]] = []
    result = await _registry(_CLAIM, seen).dispatch(
        ToolCall(id="1", name="claim", arguments={"seat": 2, "item": "Bolivia"}), _CTX
    )
    assert result.content == "ok"
    assert seen == [{"seat": 2, "item": "Bolivia"}]


async def test_harmless_spellings_are_converted_not_refused() -> None:
    """A seat sent as "2" and an item sent as a number still reach the handler, typed."""
    seen: list[dict[str, object]] = []
    result = await _registry(_CLAIM, seen).dispatch(
        ToolCall(id="1", name="claim", arguments={"seat": "2", "item": 7}), _CTX
    )
    assert result.content == "ok"
    assert seen == [{"seat": 2, "item": "7"}]


def test_coerce_scalars_leaves_what_does_not_convert() -> None:
    from core.agents.tools import coerce_scalars

    schema = {
        "properties": {
            "n": {"type": "integer"},
            "flag": {"type": "boolean"},
            "s": {"type": "string"},
            "x": {"type": "number"},
        }
    }
    assert coerce_scalars(schema, {"n": "two", "flag": "yes", "s": True, "x": "1.5"}) == {
        "n": "two",
        "flag": "yes",
        "s": "true",
        "x": 1.5,
    }


def test_an_unusable_schema_does_not_make_a_tool_uncallable() -> None:
    assert argument_errors({}, {"anything": 1}) == []
    assert argument_errors({"type": "not-a-type"}, {"anything": 1}) == []


def test_merge_turn_text_keeps_earlier_words_once() -> None:
    assert merge_turn_text([], "Done.") == "Done."
    assert (
        merge_turn_text(["Let me check."], "Yes, it worked.") == "Let me check.\n\nYes, it worked."
    )
    assert merge_turn_text(["Seat 1 is Bolivia."], "Seat 1 is Bolivia. Claiming it.") == (
        "Seat 1 is Bolivia. Claiming it."
    )
    assert merge_turn_text(["Claiming seat 1."], "") == "Claiming seat 1."


async def test_text_sent_with_a_tool_call_stays_in_the_transcript(db_available: None) -> None:
    """The pilot showed most models talk in the same response as their move. Only the
    final iteration's text used to be saved, so that talk vanished."""
    tenant_id, session_id, persona_id = await _setup("hygiene-talk")
    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="Seat 1 must be landlocked South America.",
                tool_calls=(
                    ToolCall(id="c1", name="claim", arguments={"seat": 1, "item": "Bolivia"}),
                ),
            ),
            _ScriptedTurn(text="Your move, seat 4."),
        ]
    )
    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "Your turn."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=_registry(_CLAIM, []),
        idempotency_key=f"turn:{uuid.uuid4()}",
    )
    expected = "Seat 1 must be landlocked South America.\n\nYour move, seat 4."
    assert result.content_md == expected
    async with tenant_scope(tenant_id) as session:
        stored = await session.scalar(select(MessageRow).where(MessageRow.session_id == session_id))
    assert stored is not None and stored.content_md == expected


async def test_an_unknown_tool_no_longer_ends_the_turn(db_available: None) -> None:
    tenant_id, session_id, persona_id = await _setup("hygiene-unknown")
    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(text="", tool_calls=(ToolCall(id="c1", name="guess", arguments={}),)),
            _ScriptedTurn(text="I meant to claim; I'll pass."),
        ]
    )
    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "Your turn."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=_registry(_CLAIM, []),
        idempotency_key=f"turn:{uuid.uuid4()}",
    )
    assert result.content_md == "I meant to claim; I'll pass."
    tool_reply = provider.requests[1][-1]
    assert tool_reply["role"] == "tool"
    assert json.loads(str(tool_reply["content"]))["error"] == "unknown_tool"


async def test_extra_usage_is_committed_with_the_message(db_available: None) -> None:
    """A regeneration made for the turn (the leak check's) is metered on the turn's
    message, in the same commit."""
    tenant_id, session_id, persona_id = await _setup("hygiene-usage")
    provider = _ScriptedProvider(turns=[_ScriptedTurn(text="A clean reply.")])
    extra: list[UsagePoint] = []

    async def finalize(text: str) -> str:
        extra.append(UsagePoint(uuid.uuid4(), "fake", "fake-regen", 40, 12, 0, 5))
        return text

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        session_id,
        [{"role": "user", "content": "Go."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{uuid.uuid4()}",
        finalize_reply=finalize,
        extra_usage_points=extra,
    )
    assert len(result.usage_record_ids) == 2
    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
    assert {r.model for r in rows} >= {"fake-regen"}
    assert all(r.message_id == rows[0].message_id for r in rows)
