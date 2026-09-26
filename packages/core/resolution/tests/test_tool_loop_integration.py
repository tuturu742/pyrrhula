"""A model asserting a false modifier is rejected, proven through the *real* agent tool
loop, not just a direct call to ``resolve()``: tool loop, resolution and validation end
to end.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sqlalchemy import select

from core.agents.runtime import run_agent_turn
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolRegistry
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ToolCall, ToolSpec
from core.process.skeleton import create_session
from core.resolution.records import ResolutionRecordRow
from core.resolution.rule_system import MINIMAL_D20_SYSTEM, RuleSystemDefinition, create_rule_system
from core.resolution.service import make_randomizer_handler
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass(frozen=True)
class _ScriptedTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass
class _ScriptedProvider:
    turns: list[_ScriptedTurn]
    call_count: int = field(default=0, init=False)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.call_count += 1
        turn = self.turns.pop(0)
        finish_reason = "tool_calls" if turn.tool_calls else "stop"
        yield Chunk(text=turn.text, finish_reason=finish_reason, tool_calls=turn.tool_calls)

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=True, supports_json_mode=False, supports_prompt_caching=False
        )


async def test_model_claiming_a_false_modifier_is_rejected_through_the_real_tool_loop(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"toolloop-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    rule_system_row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    rule_system = RuleSystemDefinition.from_row(rule_system_row)

    async def actor_fields_resolver(actor_entity_id: uuid.UUID | None) -> dict[str, object]:
        return {"dexterity": 16}  # true modifier is +3, not the +5 the model will claim

    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
        actor_fields_resolver=actor_fields_resolver,
    )
    registry = ToolRegistry()
    registry.register(ToolSpec(name="randomizer", description="Roll", parameters={}), handler)

    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="randomizer",
                        arguments={"expression": "1d20+5", "check_type": "stealth"},
                    ),
                ),
            ),
            _ScriptedTurn(text="Understood, I'll use the correct modifier."),
        ]
    )

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "I try to sneak past the guard."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    assert result.content_md == "Understood, I'll use the correct modifier."
    assert result.tool_calls_made == 1

    async with tenant_scope(tenant_id) as session:
        records = (
            (
                await session.execute(
                    select(ResolutionRecordRow).where(ResolutionRecordRow.session_id == sess.id)
                )
            )
            .scalars()
            .all()
        )
    # The core anti-hallucination property: no ResolutionRecord was ever written for the
    # rejected claim -- rejection happens before any "roll" occurs, not after.
    assert records == []


async def test_valid_roll_through_the_real_tool_loop_writes_exactly_one_record(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"toolloop-valid-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    rule_system_row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    rule_system = RuleSystemDefinition.from_row(rule_system_row)

    async def actor_fields_resolver(actor_entity_id: uuid.UUID | None) -> dict[str, object]:
        return {"dexterity": 16}

    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
        actor_fields_resolver=actor_fields_resolver,
    )
    registry = ToolRegistry()
    registry.register(ToolSpec(name="randomizer", description="Roll", parameters={}), handler)

    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="randomizer",
                        arguments={"expression": "1d20+3", "check_type": "stealth", "target": 10},
                    ),
                ),
            ),
            _ScriptedTurn(text="You slip past unnoticed."),
        ]
    )

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "I try to sneak past the guard."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    assert result.content_md == "You slip past unnoticed."

    async with tenant_scope(tenant_id) as session:
        records = (
            (
                await session.execute(
                    select(ResolutionRecordRow).where(ResolutionRecordRow.session_id == sess.id)
                )
            )
            .scalars()
            .all()
        )
        usage_rows = (
            (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.session_id == sess.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(records) == 1
    assert len(usage_rows) == 2  # tool-call turn + final turn -- unrelated to the roll itself


async def test_contradicting_narration_still_shows_the_records_truth_through_the_real_tool_loop(
    db_available: None,
) -> None:
    """INV-7 demonstrated: a reply narrating the wrong
    outcome doesn't change what's recorded, and gets flagged. ``target=100`` against a
    ``1d20+3`` roll (max possible total 23) makes the true outcome deterministically
    "failure" regardless of the actual roll -- the scripted reply then claims success,
    which is guaranteed to contradict without needing to control the RNG seed.
    """
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"toolloop-contradiction-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    rule_system_row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    rule_system = RuleSystemDefinition.from_row(rule_system_row)

    async def actor_fields_resolver(actor_entity_id: uuid.UUID | None) -> dict[str, object]:
        return {"dexterity": 16}

    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
        actor_fields_resolver=actor_fields_resolver,
    )
    registry = ToolRegistry()
    registry.register(ToolSpec(name="randomizer", description="Roll", parameters={}), handler)

    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="randomizer",
                        arguments={
                            "expression": "1d20+3",
                            "check_type": "stealth",
                            "target": 100,
                        },
                    ),
                ),
            ),
            _ScriptedTurn(text="Your blade connects perfectly -- you succeed!"),
        ]
    )

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "I attack the guard."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    async with tenant_scope(tenant_id) as session:
        message = await session.get(MessageRow, result.message_id)
        assert message is not None
        assert len(message.resolution_record_ids) == 1
        record = await session.get(ResolutionRecordRow, uuid.UUID(message.resolution_record_ids[0]))
        assert record is not None
        # The record is the truth: a max-23 roll against a target of 100 always fails,
        # no matter what the (mocked) narration claims.
        assert record.outcome == "failure"
        # The badge: the message's own moderation_flags points back at that same record.
        assert message.moderation_flags == {"contradiction": [str(record.id)]}
