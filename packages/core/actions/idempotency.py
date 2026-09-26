"""The ``@idempotent`` decorator. Every side-effecting operation — model call,
tool execution, entity mutation, external MCP call — takes an idempotency key derived
from ``(session_id, event_seq, attempt_target)``. On retry or resume, the key hits
``completed_operation`` and returns the stored result rather than re-executing.

This is not optional and it is not deferrable: resume-from-checkpoint
re-executes work, so a node that made a paid API call before an interrupt point charges
the tenant's key twice on resume unless every side-effecting call site goes through this.

**Claim before execute, not cache after.** A naive "check cache, run, write cache" allows
two concurrent callers to both pass the check and both run the side effect. This decorator
instead does an atomic ``INSERT ... ON CONFLICT DO NOTHING`` *before* running the wrapped
function: whoever's insert lands owns the execution; everyone else polls the row until it
reaches a terminal status and returns that result instead of running anything.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from core.actions.models import CompletedOperationRow
from core.tenancy.scope import tenant_scope

P = ParamSpec("P")
R = TypeVar("R")

_DEFAULT_POLL_INTERVAL = 0.05
_DEFAULT_TIMEOUT = 5.0


class OperationFailedError(Exception):
    """The operation that owned this idempotency key raised; re-raising the original
    exception across the poll boundary isn't possible, so callers get this instead."""


class OperationTimeoutError(Exception):
    """Nobody finished the claimed operation within the poll timeout."""


def idempotent(
    key_fn: Callable[..., str],
    *,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Callable[[Callable[P, Awaitable[dict[str, Any]]]], Callable[P, Awaitable[dict[str, Any]]]]:
    """``key_fn`` receives the same arguments as the wrapped function and must return a
    stable string key. The wrapped function must be called with a ``tenant_id: uuid.UUID``
    keyword argument (idempotency records are tenant-scoped) and must return a JSON-able
    dict.
    """

    def decorator(
        wrapped_func: Callable[P, Awaitable[dict[str, Any]]],
    ) -> Callable[P, Awaitable[dict[str, Any]]]:
        @functools.wraps(wrapped_func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
            tenant_id = kwargs.get("tenant_id")
            if not isinstance(tenant_id, uuid.UUID):
                raise TypeError("@idempotent functions require a tenant_id: uuid.UUID kwarg")

            key = key_fn(*args, **kwargs)

            async with tenant_scope(tenant_id) as session:
                claim = await session.execute(
                    insert(CompletedOperationRow)
                    .values(idempotency_key=key, tenant_id=tenant_id, status="in_progress")
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    .returning(CompletedOperationRow.idempotency_key)
                )
                won_claim = claim.first() is not None

            if not won_claim:
                # Someone else holds the claim. Usually that someone is alive and we wait
                # for their result -- but a process killed mid-operation leaves its row at
                # 'in_progress' for ever, and nothing else ever writes it. Every retry then
                # polls a row that will never change and dies on the timeout, so a session
                # whose worker was killed mid-turn could never be resumed: rule 8 says
                # resume re-executes, and this made resume impossible instead.
                #
                # A claim older than the lease is treated as abandoned and taken over. The
                # takeover is conditional on it still being stale, so two reclaimers race
                # for one winner exactly as the original insert does.
                if await _reclaim_if_abandoned(key, tenant_id):
                    won_claim = True
                else:
                    return await _await_result(key, tenant_id, poll_interval, timeout)

            if not won_claim:  # pragma: no cover -- defensive, both branches set it
                return await _await_result(key, tenant_id, poll_interval, timeout)

            try:
                result = await wrapped_func(*args, **kwargs)
            except Exception:
                async with tenant_scope(tenant_id) as session:
                    await session.execute(
                        update(CompletedOperationRow)
                        .where(CompletedOperationRow.idempotency_key == key)
                        .values(status="failed", completed_at=func.now())
                    )
                raise

            async with tenant_scope(tenant_id) as session:
                await session.execute(
                    update(CompletedOperationRow)
                    .where(CompletedOperationRow.idempotency_key == key)
                    .values(status="done", result=result, completed_at=func.now())
                )
            return result

        return wrapper

    return decorator


async def clear_failed_operation(tenant_id: uuid.UUID, idempotency_key: str) -> bool:
    """`@idempotent` marks a key `status="failed"` permanently on any exception --
    correct for a genuine duplicate-side-effect guard, but a real problem once the
    wrapped operation is something that can fail *transiently* (a model provider outage
    inside `run_agent_turn`, reached through the interpreter's own turn-execution key).
    Without an escape hatch, one transient failure permanently stalls whatever's keyed
    on it. Deletes the row only if it's actually `"failed"` -- never touches an
    in-progress or done row, so this can never un-poison a genuinely-completed
    operation or race a concurrently-running one. Returns whether a row was actually
    cleared, so a caller (`core.sessions.lifecycle.resume_session`) can tell "there was
    something to retry" from "nothing needed clearing"."""
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            delete(CompletedOperationRow).where(
                CompletedOperationRow.idempotency_key == idempotency_key,
                CompletedOperationRow.status == "failed",
            )
        )
        rowcount: int = result.rowcount  # type: ignore[attr-defined]
        return rowcount > 0


# How long an operation may sit 'in_progress' before another caller may take it over. It
# has to exceed the longest legitimate operation -- a model turn with a tool loop -- or a
# slow turn gets executed twice. Fifteen minutes is well past any single turn and well
# short of a person noticing a stuck session.
CLAIM_LEASE_SECONDS = 900
# Kept as the private spelling this module already used everywhere.
_CLAIM_LEASE_SECONDS = CLAIM_LEASE_SECONDS


async def _reclaim_if_abandoned(key: str, tenant_id: uuid.UUID) -> bool:
    """Take over a claim nobody is working on. True if this caller won it.

    Only an 'in_progress' row older than the lease qualifies: a live holder finishes or
    fails within the lease, so one older than that has no holder left.

    A 'failed' row is deliberately NOT reclaimed here. Failure is sticky by design --
    an operation that failed after its side effect landed must not be re-run just
    because someone asked again -- and ``clear_failed_operation`` is the explicit way to
    say "that failure was the deployment's fault, run it again".
    """
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=_CLAIM_LEASE_SECONDS)
    async with tenant_scope(tenant_id) as session:
        claimed = await session.execute(
            update(CompletedOperationRow)
            .where(
                CompletedOperationRow.idempotency_key == key,
                CompletedOperationRow.status == "in_progress",
                CompletedOperationRow.created_at < cutoff,
            )
            # A fresh timestamp under this caller, so a second reclaimer loses the race
            # rather than running the same operation alongside it.
            .values(created_at=func.now())
            .returning(CompletedOperationRow.idempotency_key)
        )
        return claimed.first() is not None


async def _await_result(
    key: str, tenant_id: uuid.UUID, poll_interval: float, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(CompletedOperationRow).where(CompletedOperationRow.idempotency_key == key)
            )
        if row is not None:
            if row.status == "done":
                return row.result or {}
            if row.status == "failed":
                # The key alone told a reader nothing: a worker log line reading
                # "error=turn:<uuid>:6" names the operation and not the problem, and
                # sends whoever reads it looking for a turn rather than for the
                # recorded failure of one.
                raise OperationFailedError(
                    f"{key}: a previous attempt failed and the failure is recorded. "
                    f"Clear it with clear_failed_operation to retry "
                    f"(resume_session does this for the turn it retries)."
                )
        await asyncio.sleep(poll_interval)
    raise OperationTimeoutError(
        f"{key}: still in progress after {timeout}s. Another caller holds this "
        f"operation; if its process died, the claim is taken over after "
        f"{_CLAIM_LEASE_SECONDS}s."
    )
