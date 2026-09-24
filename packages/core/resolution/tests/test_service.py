"""C1.6 acceptance criteria for the resolution trust chain, against a live Postgres."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolContext
from core.config import get_settings
from core.process.skeleton import create_session
from core.resolution.grammar import parse_expression
from core.resolution.records import ResolutionRecordRow, roll_expression, verify_resolution_chain
from core.resolution.registry import RANDOMIZER_DEFINITION, register_tool_definition
from core.resolution.rule_system import (
    COIN_FLIP_SYSTEM,
    MINIMAL_D20_SYSTEM,
    RuleSystemDefinition,
    create_rule_system,
)
from core.resolution.service import (
    InvalidResolutionError,
    make_randomizer_handler,
    render_resolution_fact,
    resolve,
)
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, RuleSystemDefinition, uuid.UUID]:
    """Returns (tenant_id, session_id, rule_system_definition, rule_system_id)."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    rule_system_row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    return tenant_id, sess.id, RuleSystemDefinition.from_row(rule_system_row), rule_system_row.id


# ── the anti-hallucination property itself (also covered at the validate() level in
# C1.5; here it's proven end to end through resolve(), including "no record written") ──


async def test_invalid_resolution_raises_and_writes_no_record(db_available: None) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-invalid")

    with pytest.raises(InvalidResolutionError) as exc_info:
        await resolve(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=0,
            tool_key="randomizer",
            actor_entity_id=None,
            expression="1d20+5",
            check_type="stealth",
            actor_fields={"dexterity": 16},  # actual modifier is +3
            target=15,
            rule_system=rule_system,
            rule_system_id=rule_system_id,
            legal_check_types=None,
        )
    assert exc_info.value.error.code == "modifier_mismatch"
    assert exc_info.value.error.expected_modifier == 3

    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(ResolutionRecordRow).where(ResolutionRecordRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_valid_resolution_writes_a_record_with_the_computed_outcome(
    db_available: None,
) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-valid")

    record = await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="stealth",
        actor_fields={"dexterity": 16},
        target=10,
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
    )

    assert record.total == sum(record.rolls) + 3
    assert record.outcome == ("success" if record.total >= 10 else "failure")
    assert record.rule_system_id == rule_system_id


# ── seed disclosure reproduces the recorded rolls exactly ──────────────────────────


async def test_seed_disclosure_reproduces_the_recorded_rolls_exactly(db_available: None) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-seed-disclosure")

    record = await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="stealth",
        actor_fields={"dexterity": 16},
        target=10,
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
    )

    # Simulate disclosing the seed to a player post-session: independently recompute the
    # roll from nothing but the disclosed seed + the recorded expression/modifier.
    parsed = parse_expression(record.expression)
    modifier = record.modifiers["total"]
    assert isinstance(modifier, int)
    recomputed = roll_expression(parsed, modifier, bytes.fromhex(record.seed))

    assert list(recomputed.rolls) == record.rolls
    assert recomputed.total == record.total


# ── hash chain verifier detects a tampered record ───────────────────────────────────


async def test_hash_chain_verifier_detects_a_tampered_record(db_available: None) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-tamper")

    await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="stealth",
        actor_fields={"dexterity": 16},
        target=10,
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
    )
    record_two = await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=1,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="stealth",
        actor_fields={"dexterity": 16},
        target=10,
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
    )

    async with tenant_scope(tenant_id) as session:
        rows_before = (
            (
                await session.execute(
                    select(ResolutionRecordRow).where(ResolutionRecordRow.session_id == session_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows_before) == 2

    # pyrrhula_app is granted no UPDATE on resolution_record at all (append-only) --
    # reaching this row for a tamper simulation requires the admin/migrator role
    # directly, matching test_service_and_verify.py's identical pattern for audit_log.
    admin_engine = create_async_engine(get_settings().database_url)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("UPDATE resolution_record SET total = 9999 WHERE id = :id"),
                {"id": record_two.id},
            )
    finally:
        await admin_engine.dispose()

    async with tenant_scope(tenant_id) as session:
        broken = await verify_resolution_chain(session, session_id)
    assert broken == [record_two.id]


async def test_app_role_cannot_update_or_delete_resolution_record(db_available: None) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-grant")
    record = await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="stealth",
        actor_fields={"dexterity": 16},
        target=10,
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
    )

    async with tenant_scope(tenant_id) as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE resolution_record SET total = 1 WHERE id = :id"), {"id": record.id}
            )


# ── retry storm produces exactly one record per logical roll ───────────────────────


async def test_retry_storm_produces_exactly_one_record_per_logical_roll(
    db_available: None,
) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-retry-storm")

    async def _attempt() -> ResolutionRecordRow:
        return await resolve(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=0,  # same event_seq every attempt -- the same "logical roll"
            tool_key="randomizer",
            actor_entity_id=None,
            expression="1d20+3",
            check_type="stealth",
            actor_fields={"dexterity": 16},
            target=10,
            rule_system=rule_system,
            rule_system_id=rule_system_id,
            legal_check_types=None,
        )

    results = await asyncio.gather(*[_attempt() for _ in range(10)])

    assert len({r.id for r in results}) == 1  # every concurrent attempt got the SAME record

    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(ResolutionRecordRow).where(
                        ResolutionRecordRow.session_id == session_id,
                        ResolutionRecordRow.event_seq == 0,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


# ── system-authored fact rendering (§9.2 step 5) ────────────────────────────────────


async def test_render_resolution_fact_is_authoritative_and_instructs_no_contradiction(
    db_available: None,
) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-fact")
    record = await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="stealth",
        actor_fields={"dexterity": 16},
        target=10,
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
    )

    fact = render_resolution_fact(record, check_type="stealth")

    assert f'id="{record.id}"' in fact
    assert 'authoritative="true"' in fact
    assert str(record.total) in fact
    assert record.outcome.upper() in fact
    assert "may not contradict" in fact


# ── one tool, many rule systems (the collapse of the separate coin-flip tool) ──────


async def _coin_system(tenant_id: uuid.UUID) -> uuid.UUID:
    row = await create_rule_system(tenant_id, COIN_FLIP_SYSTEM)
    return row.id


async def _run(handler, args: dict[str, object], tenant_id: uuid.UUID, session_id: uuid.UUID):
    ctx = ToolContext(tenant_id=tenant_id, persona_id=uuid.uuid4(), session_id=session_id)
    result = await handler(args, ctx)
    return json.loads(result.content)


async def test_one_call_selects_the_coin_system_and_another_the_default(
    db_available: None,
) -> None:
    """The reason a separate `coin_flip` *tool* was deleted: both were the same handler
    over the same builtin, differing only in which rule system validated the roll. A
    caller names the system per call instead, so one turn can do both."""
    tenant_id, session_id, rule_system, rule_system_id = await _setup("selector-both")
    await _coin_system(tenant_id)

    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=rule_system.check_types,
        actor_fields_resolver=_fixed_fields,
    )

    flip = await _run(
        handler,
        {"expression": "1d2", "check_type": "call", "rule_system": "coin_flip"},
        tenant_id,
        session_id,
    )
    assert flip["outcome"] in ("heads", "tails"), flip

    check = await _run(
        handler,
        {"expression": "1d20", "check_type": "strength_check", "target": 10},
        tenant_id,
        session_id,
    )
    assert check["outcome"] in ("success", "failure"), check


async def test_a_coin_expression_is_refused_by_the_default_system(db_available: None) -> None:
    """Selection is load-bearing, not decorative: without it the d20 grammar rejects
    `1d2`, which is exactly why resolving a coin in the session default would be wrong
    rather than merely untidy."""
    tenant_id, session_id, rule_system, rule_system_id = await _setup("selector-refused")
    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=rule_system.check_types,
        actor_fields_resolver=_fixed_fields,
    )

    out = await _run(
        handler, {"expression": "1d2", "check_type": "strength_check"}, tenant_id, session_id
    )
    assert out["error"] == "illegal_expression", out


async def test_an_unregistered_rule_system_is_an_error_not_a_silent_fallback(
    db_available: None,
) -> None:
    """Falling back to the default would write a plausible record for a roll nobody
    asked for -- the failure mode INV-7 exists to prevent."""
    tenant_id, session_id, rule_system, rule_system_id = await _setup("selector-unknown")
    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=rule_system.check_types,
        actor_fields_resolver=_fixed_fields,
    )

    out = await _run(
        handler,
        {"expression": "1d20", "check_type": "strength_check", "rule_system": "no_such_system"},
        tenant_id,
        session_id,
    )
    assert out["error"] == "unknown_rule_system", out


async def test_the_tool_definitions_validation_ref_binds_when_no_selector_is_given(
    db_available: None,
) -> None:
    """A pack that wants its randomizer to always resolve in one system says so with
    `validation_ref`, and does not have to repeat itself on every call."""
    tenant_id, session_id, rule_system, rule_system_id = await _setup("selector-bound")
    await _coin_system(tenant_id)
    await register_tool_definition(
        tenant_id, RANDOMIZER_DEFINITION.model_copy(update={"validation_ref": "coin_flip"})
    )

    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=rule_system.check_types,
        actor_fields_resolver=_fixed_fields,
    )

    out = await _run(handler, {"expression": "1d2", "check_type": "call"}, tenant_id, session_id)
    assert out["outcome"] in ("heads", "tails"), out


async def _fixed_fields(_actor_entity_id: uuid.UUID | None) -> dict[str, object]:
    return {"strength": 16, "dexterity": 14}


async def test_a_malformed_actor_id_costs_the_call_and_not_the_session(
    db_available: None,
) -> None:
    """Every argument here is model output, so every one is a thing a model can get
    wrong. Unguarded, a mistyped id raised out of the handler, failed the advance job,
    and recorded the turn as failed -- after which the idempotency guard correctly
    refused to retry it. A campaign died at its boss fight that way, ninety minutes in,
    on one malformed uuid.
    """
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-bad-actor")
    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
        actor_fields_resolver=_fixed_fields,
    )
    ctx = ToolContext(tenant_id=tenant_id, persona_id=uuid.uuid4(), session_id=session_id)

    result = await handler(
        {
            "expression": "1d20",
            "check_type": sorted(rule_system.check_types)[0],
            "actor_entity_id": "not-a-uuid",
        },
        ctx,
    )

    payload = json.loads(result.content)
    assert payload["error"] == "invalid_args"
    assert "not a uuid" in payload["message"]


async def test_a_missing_expression_is_answered_not_raised(db_available: None) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-no-expr")
    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=None,
        actor_fields_resolver=_fixed_fields,
    )
    ctx = ToolContext(tenant_id=tenant_id, persona_id=uuid.uuid4(), session_id=session_id)

    payload = json.loads(
        (await handler({"check_type": sorted(rule_system.check_types)[0]}, ctx)).content
    )
    assert payload["error"] == "missing_args"
    assert "expression" in payload["message"]
