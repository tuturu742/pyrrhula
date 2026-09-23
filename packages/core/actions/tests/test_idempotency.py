import asyncio
import uuid

import pytest

from core.actions.idempotency import OperationFailedError, clear_failed_operation, idempotent
from core.tenancy.seed import seed_dev_tenant


async def test_second_call_returns_cached_result_without_rerunning(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"idem-{uuid.uuid4().hex[:8]}")
    calls = 0

    @idempotent(key_fn=lambda **kw: kw["key"])
    async def do_work(*, key: str, tenant_id: uuid.UUID) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    key = f"op-{uuid.uuid4()}"
    result1 = await do_work(key=key, tenant_id=tenant_id)
    result2 = await do_work(key=key, tenant_id=tenant_id)

    assert calls == 1
    assert result1 == result2 == {"value": 1}


async def test_different_keys_both_execute(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"idem-{uuid.uuid4().hex[:8]}")
    calls = 0

    @idempotent(key_fn=lambda **kw: kw["key"])
    async def do_work(*, key: str, tenant_id: uuid.UUID) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    await do_work(key=f"a-{uuid.uuid4()}", tenant_id=tenant_id)
    await do_work(key=f"b-{uuid.uuid4()}", tenant_id=tenant_id)

    assert calls == 2


async def test_concurrent_calls_execute_side_effect_exactly_once(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"idem-{uuid.uuid4().hex[:8]}")
    calls = 0

    @idempotent(key_fn=lambda **kw: kw["key"])
    async def do_work(*, key: str, tenant_id: uuid.UUID) -> dict[str, object]:
        nonlocal calls
        calls += 1
        # Hold the "in_progress" claim for a bit so concurrent callers actually race
        # against the claim, not against a call that's already finished.
        await asyncio.sleep(0.2)
        return {"value": "the one true result"}

    key = f"race-{uuid.uuid4()}"
    results = await asyncio.gather(*(do_work(key=key, tenant_id=tenant_id) for _ in range(10)))

    assert calls == 1
    assert all(r == {"value": "the one true result"} for r in results)


async def test_failed_operation_raises_for_waiters(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"idem-{uuid.uuid4().hex[:8]}")

    @idempotent(key_fn=lambda **kw: kw["key"])
    async def do_work(*, key: str, tenant_id: uuid.UUID) -> dict[str, object]:
        await asyncio.sleep(0.1)
        raise RuntimeError("boom")

    key = f"fail-{uuid.uuid4()}"

    async def runner() -> None:
        await do_work(key=key, tenant_id=tenant_id)

    results = await asyncio.gather(runner(), runner(), return_exceptions=True)
    assert any(isinstance(r, RuntimeError) for r in results)
    assert any(isinstance(r, OperationFailedError) for r in results)


async def test_clear_failed_operation_lets_a_retry_actually_rerun(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"idem-clear-{uuid.uuid4().hex[:8]}")
    calls = 0
    should_fail = True

    @idempotent(key_fn=lambda **kw: kw["key"])
    async def do_work(*, key: str, tenant_id: uuid.UUID) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if should_fail:
            raise RuntimeError("transient failure")
        return {"value": "recovered"}

    key = f"clear-{uuid.uuid4()}"
    with pytest.raises(RuntimeError):
        await do_work(key=key, tenant_id=tenant_id)
    assert calls == 1

    # Without clearing, the same key just re-raises OperationFailedError forever --
    # never re-executes, even once the underlying condition (should_fail) is gone.
    should_fail = False
    with pytest.raises(OperationFailedError):
        await do_work(key=key, tenant_id=tenant_id)
    assert calls == 1

    cleared = await clear_failed_operation(tenant_id, key)
    assert cleared is True

    result = await do_work(key=key, tenant_id=tenant_id)
    assert calls == 2
    assert result == {"value": "recovered"}


async def test_clear_failed_operation_is_a_noop_for_a_done_or_missing_key(
    db_available: None,
) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"idem-clear-noop-{uuid.uuid4().hex[:8]}")

    @idempotent(key_fn=lambda **kw: kw["key"])
    async def do_work(*, key: str, tenant_id: uuid.UUID) -> dict[str, object]:
        return {"value": "done"}

    done_key = f"done-{uuid.uuid4()}"
    await do_work(key=done_key, tenant_id=tenant_id)

    assert await clear_failed_operation(tenant_id, done_key) is False
    assert await clear_failed_operation(tenant_id, f"missing-{uuid.uuid4()}") is False

    # The done result is still there -- clearing a non-failed key is truly a no-op.
    result = await do_work(key=done_key, tenant_id=tenant_id)
    assert result == {"value": "done"}


async def test_requires_tenant_id_kwarg() -> None:
    @idempotent(key_fn=lambda **kw: "k")
    async def do_work(*, tenant_id: uuid.UUID) -> dict[str, object]:
        return {}

    with pytest.raises(TypeError):
        await do_work(tenant_id="not-a-uuid")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_operation_whose_process_died_can_be_retried(db_available: None) -> None:
    """Rule 8 says resume re-executes. A process killed mid-operation leaves its claim at
    'in_progress' and nothing else ever writes that row, so before this every retry polled
    a row that would never change and failed on the timeout -- with the idempotency key as
    the entire error message.

    A tabletop session whose worker was killed by the OOM reaper could not be resumed at
    all: not slow, not degraded, impossible.
    """
    import uuid as _uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from core.actions.idempotency import _CLAIM_LEASE_SECONDS, idempotent
    from core.actions.models import CompletedOperationRow
    from core.tenancy.scope import tenant_scope
    from core.tenancy.seed import seed_dev_tenant

    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"idem-{_uuid.uuid4().hex[:8]}")
    key = f"turn:{_uuid.uuid4()}"
    calls: list[int] = []

    @idempotent(key_fn=lambda **kw: key)
    async def operation(*, tenant_id: _uuid.UUID) -> dict[str, object]:
        calls.append(1)
        return {"ran": len(calls)}

    # A claim left behind by a process that never came back.
    async with tenant_scope(tenant_id) as session:
        session.add(
            CompletedOperationRow(idempotency_key=key, tenant_id=tenant_id, status="in_progress")
        )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            update(CompletedOperationRow)
            .where(CompletedOperationRow.idempotency_key == key)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=_CLAIM_LEASE_SECONDS + 60))
        )

    assert await operation(tenant_id=tenant_id) == {"ran": 1}


@pytest.mark.asyncio
async def test_a_live_claim_is_still_waited_for(db_available: None) -> None:
    """The lease only frees an abandoned claim. A claim made a moment ago belongs to a
    caller that is probably still working, and taking it over would run the operation
    twice -- which for a model turn means charging for it twice."""
    import uuid as _uuid

    from core.actions.idempotency import _reclaim_if_abandoned
    from core.actions.models import CompletedOperationRow
    from core.tenancy.scope import tenant_scope
    from core.tenancy.seed import seed_dev_tenant

    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"idem-{_uuid.uuid4().hex[:8]}")
    key = f"turn:{_uuid.uuid4()}"
    async with tenant_scope(tenant_id) as session:
        session.add(
            CompletedOperationRow(idempotency_key=key, tenant_id=tenant_id, status="in_progress")
        )

    assert await _reclaim_if_abandoned(key, tenant_id) is False
