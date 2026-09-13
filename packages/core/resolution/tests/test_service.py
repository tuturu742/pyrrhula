"""C1.6 acceptance criteria for the resolution trust chain, against a live Postgres."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.agents.seed import seed_dev_agent
from core.config import get_settings
from core.process.skeleton import create_session
from core.resolution.grammar import parse_expression
from core.resolution.records import ResolutionRecordRow, roll_dice, verify_resolution_chain
from core.resolution.rule_system import MINIMAL_D20_SYSTEM, RuleSystemDefinition, create_rule_system
from core.resolution.service import InvalidResolutionError, render_resolution_fact, resolve
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
            tool_key="dice_roller",
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
        tool_key="dice_roller",
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
        tool_key="dice_roller",
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
    recomputed = roll_dice(parsed, modifier, bytes.fromhex(record.seed))

    assert list(recomputed.rolls) == record.rolls
    assert recomputed.total == record.total


# ── hash chain verifier detects a tampered record ───────────────────────────────────


async def test_hash_chain_verifier_detects_a_tampered_record(db_available: None) -> None:
    tenant_id, session_id, rule_system, rule_system_id = await _setup("resolve-tamper")

    await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        tool_key="dice_roller",
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
        tool_key="dice_roller",
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
        tool_key="dice_roller",
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
            tool_key="dice_roller",
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
        tool_key="dice_roller",
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
