"""C1.7 acceptance criteria for the contradiction scanner."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.agents.seed import seed_dev_agent
from core.process.skeleton import create_session, submit_user_message
from core.resolution.contradiction import (
    ContradictionFlag,
    contradiction_rate,
    flag_contradictions,
    scan_for_contradictions,
)
from core.resolution.records import ResolutionRecordRow
from core.resolution.rule_system import MINIMAL_D20_SYSTEM, RuleSystemDefinition, create_rule_system
from core.resolution.service import resolve
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _fake_record(*, total: int, outcome: str) -> ResolutionRecordRow:
    """A record built without a DB round-trip -- scan_for_contradictions() is a pure
    function over records + text, so it doesn't need one."""
    return ResolutionRecordRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        event_seq=0,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        seed="ab" * 32,
        rolls=[16],
        modifiers={"total": 3},
        total=total,
        target=10,
        outcome=outcome,
        rule_system_id=uuid.uuid4(),
        rule_citation_ids=[],
        prev_hash=None,
        row_hash="cd" * 32,
        created_at=datetime.now(UTC),
    )


# ── the three literal fixtures from the task's own acceptance criterion ────────────


def test_barely_fail_against_a_success_record_is_flagged() -> None:
    record = _fake_record(total=19, outcome="success")
    flags = scan_for_contradictions("You barely fail to pick the lock.", [record])

    assert len(flags) == 1
    assert flags[0].record_id == record.id
    assert flags[0].reason == "outcome_word_mismatch"


def test_faithful_narration_is_not_flagged() -> None:
    record = _fake_record(total=19, outcome="success")
    flags = scan_for_contradictions("You barely succeed, slipping the lock open.", [record])
    assert flags == []


def test_narration_not_mentioning_the_roll_is_not_flagged() -> None:
    record = _fake_record(total=19, outcome="success")
    flags = scan_for_contradictions("The torchlight flickers down the corridor.", [record])
    assert flags == []


# ── additional coverage: numerals, ambiguity, banded outcomes ──────────────────────


def test_stated_total_mismatch_is_flagged() -> None:
    record = _fake_record(total=19, outcome="success")
    flags = scan_for_contradictions("You rolled a 12, total of 12, and just make it.", [record])
    # "12" != 19 -- but note "make it" isn't in the outcome-word lexicon, so only the
    # total-mismatch signal fires here.
    assert any(f.reason == "total_mismatch" for f in flags)


def test_stray_number_unrelated_to_the_roll_is_not_flagged() -> None:
    record = _fake_record(total=19, outcome="success")
    flags = scan_for_contradictions("You find 12 gold coins and succeed at the check.", [record])
    assert flags == []  # "12 gold coins" is never mistaken for a claimed total


def test_ambiguous_text_mentioning_both_outcomes_is_not_flagged() -> None:
    record = _fake_record(total=19, outcome="success")
    flags = scan_for_contradictions(
        "It's not a clean success, but you don't entirely fail either.", [record]
    )
    assert flags == []  # both outcome families mentioned -- no signal, conservative


def test_banded_outcome_is_never_flagged_by_the_word_heuristic() -> None:
    """A banded outcome has no reliable word-level signature -- skipped
    entirely, never a false positive."""
    record = _fake_record(total=8, outcome="partial_success")
    flags = scan_for_contradictions("You fail to convince the guard.", [record])
    assert flags == []


def test_multiple_records_only_the_contradicted_one_is_flagged() -> None:
    matching = _fake_record(total=19, outcome="success")
    contradicted = _fake_record(total=5, outcome="failure")
    flags = scan_for_contradictions("You succeed with ease.", [matching, contradicted])

    assert len(flags) == 1
    assert flags[0].record_id == contradicted.id


# ── persistence + rate metric, against a live Postgres ─────────────────────────────


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, sess.id


async def test_flag_contradictions_writes_moderation_flags_on_the_message(
    db_available: None,
) -> None:
    tenant_id, session_id = await _setup("contradiction-flag")
    message = await submit_user_message(tenant_id, session_id, uuid.uuid4(), "irrelevant")

    record = _fake_record(total=19, outcome="success")
    await flag_contradictions(
        tenant_id,
        message.id,
        [ContradictionFlag(record_id=record.id, reason="outcome_word_mismatch", detail="x")],
    )

    async with tenant_scope(tenant_id) as session:
        row = await session.get(MessageRow, message.id)
        assert row is not None
        assert row.moderation_flags["contradiction"] == [str(record.id)]


async def test_flag_contradictions_is_a_noop_when_no_flags(db_available: None) -> None:
    tenant_id, session_id = await _setup("contradiction-noop")
    message = await submit_user_message(tenant_id, session_id, uuid.uuid4(), "irrelevant")

    await flag_contradictions(tenant_id, message.id, [])

    async with tenant_scope(tenant_id) as session:
        row = await session.get(MessageRow, message.id)
        assert row is not None
        assert row.moderation_flags == {}


async def test_contradiction_rate_computed_from_flagged_assistant_messages(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"contradiction-rate-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    rule_system_row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    rule_system = RuleSystemDefinition.from_row(rule_system_row)

    async def _make_assistant_message(content: str, event_seq: int) -> MessageRow:
        async with tenant_scope(tenant_id) as session:
            msg = MessageRow(
                tenant_id=tenant_id,
                session_id=sess.id,
                event_seq=event_seq,
                author_principal_id=persona_id,
                role="assistant",
                content_md=content,
            )
            session.add(msg)
            await session.flush()
            return msg

    record = await resolve(
        tenant_id=tenant_id,
        session_id=sess.id,
        event_seq=100,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="stealth",
        actor_fields={"dexterity": 16},
        target=10,
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
    )

    await _make_assistant_message("faithful narration", 0)
    msg_b = await _make_assistant_message("contradicted narration", 1)
    await _make_assistant_message("another faithful one", 2)

    assert await contradiction_rate(tenant_id, sess.id) == pytest.approx(0.0)

    flags = scan_for_contradictions(
        "you barely fail" if record.outcome == "success" else "you succeed easily", [record]
    )
    assert flags  # sanity: the scripted narration really does contradict this record
    await flag_contradictions(tenant_id, msg_b.id, flags)

    rate = await contradiction_rate(tenant_id, sess.id)
    assert rate == pytest.approx(1 / 3)
