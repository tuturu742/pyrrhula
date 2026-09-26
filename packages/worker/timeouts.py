"""Timeout sweep: fires the ``on_timeout`` transition for
any ``await_state`` row whose ``timeout_at`` has passed and that hasn't already been
satisfied. Run via ``python -m worker.timeouts`` (a separate small loop, not a
``JobQueue`` handler -- see ``core.process.awaits``'s module docstring for why a periodic
per-tenant sweep, not the job queue, is how this gets safe cross-tenant reach without
touching CLAUDE.md's closed no-RLS exception list).

A second, symmetric sweep runs on the same tick: ``sweep_due_reminders`` sends the
one configured nudge for any await whose ``reminder_at`` has passed and that is still
unresolved. Same per-tenant-scoped shape as the timeout sweep for the same RLS reason, and
exactly-once comes from the notification table's UNIQUE ``dedupe_key`` rather than from
this loop being careful -- so two sweepers, or a restarted one, still produce one nudge.

**Only fires the transition itself; does not continue running the interpreter past it.**
`resolve_timeout` moves the session to `on_timeout_phase` and clears `status='awaiting'`
completely and correctly -- but actually taking further interpreter steps from there
(running `advance_session`/`advance_session_locked`) needs a real scheduler
(`next_actor_fn`) and agent runtime (`execute_turn`), neither of which exist yet outside
test doubles. Whatever eventually calls `advance_session_locked` for a live
session (the HTTP layer) will simply see the session already
sitting in its post-timeout phase, ready to continue, the next time it runs -- no state is
lost by not chaining automatically here.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from core.observability.otel import configure_tracing, get_tracer
from core.ports.notifier import Notifier
from core.process.awaits import resolve_timeout
from core.sessions.models import AwaitStateRow, SessionRow
from core.sessions.notifications import notify_await_reminder
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope, unscoped_session
from worker.notifier_factory import get_notifier

log = structlog.get_logger()
_tracer = get_tracer(__name__)

_SWEEP_INTERVAL_SECONDS = 5.0


async def _due_await_ids_for_tenant(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(AwaitStateRow.id).where(
                    AwaitStateRow.outcome.is_(None),
                    AwaitStateRow.timeout_at < datetime.now(UTC),
                )
            )
        ).scalars()
        return list(rows)


async def sweep_expired_awaits_for_tenant(tenant_id: uuid.UUID) -> int:
    """One tenant's worth of the timeout sweep. Split out from the cross-tenant loop
     because "sweep this one tenant" is a real operation on its own -- retrying a
    tenant whose sweep failed, or draining one before a migration, shouldn't require
    walking every other tenant in the deployment."""
    resolved = 0
    for await_id in await _due_await_ids_for_tenant(tenant_id):
        on_timeout_phase = await resolve_timeout(tenant_id, await_id)
        if on_timeout_phase is not None:
            resolved += 1
            log.info(
                "timeouts.resolved",
                await_state_id=str(await_id),
                tenant_id=str(tenant_id),
                on_timeout_phase=on_timeout_phase,
            )
    return resolved


async def sweep_expired_awaits() -> int:
    """One sweep tick: for every tenant, resolve every expired-but-unresolved await.
    Returns the count actually resolved by *this* call (a concurrent sweeper, or a
    satisfy_await racing in, may have already claimed some -- resolve_timeout's atomic
    UPDATE makes that safe, just not double-counted here)."""
    with _tracer.start_as_current_span("timeouts.sweep_expired_awaits") as span:
        async with unscoped_session() as session:
            tenant_ids = list((await session.execute(select(Tenant.id))).scalars())

        resolved = sum([await sweep_expired_awaits_for_tenant(t) for t in tenant_ids])

        span.set_attribute("pyrrhula.timeouts.resolved", resolved)
        span.set_attribute("pyrrhula.timeouts.tenants_scanned", len(tenant_ids))
        return resolved


async def _due_reminders_for_tenant(
    tenant_id: uuid.UUID,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """``(await_state_id, workspace_id)`` for every unresolved await whose nudge is due.
    The workspace comes along because recipient resolution is a workspace-membership
    question and ``await_state`` doesn't carry one."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(AwaitStateRow.id, SessionRow.workspace_id)
                .join(SessionRow, SessionRow.id == AwaitStateRow.session_id)
                .where(
                    AwaitStateRow.outcome.is_(None),
                    AwaitStateRow.reminder_at.is_not(None),
                    AwaitStateRow.reminder_at < datetime.now(UTC),
                )
            )
        ).all()
        return [(row[0], row[1]) for row in rows]


async def sweep_due_reminders_for_tenant(tenant_id: uuid.UUID, notifier: Notifier) -> int:
    """One tenant's worth of the reminder sweep -- see
    ``sweep_expired_awaits_for_tenant`` for why the split exists."""
    sent = 0
    for await_id, workspace_id in await _due_reminders_for_tenant(tenant_id):
        rows = await notify_await_reminder(tenant_id, workspace_id, await_id, notifier=notifier)
        sent += len(rows)
        if rows:
            log.info(
                "timeouts.reminder_sent",
                await_state_id=str(await_id),
                tenant_id=str(tenant_id),
                recipients=len(rows),
            )
    return sent


async def sweep_due_reminders(notifier: Notifier) -> int:
    """One reminder sweep tick. Returns the number of notifications this call actually
    sent -- an await whose nudge another sweeper already claimed contributes zero, because
    ``record_notification`` returned ``None`` for it, which is the system working."""
    with _tracer.start_as_current_span("timeouts.sweep_due_reminders") as span:
        async with unscoped_session() as session:
            tenant_ids = list((await session.execute(select(Tenant.id))).scalars())

        sent = sum([await sweep_due_reminders_for_tenant(t, notifier) for t in tenant_ids])

        span.set_attribute("pyrrhula.timeouts.reminders_sent", sent)
        return sent


async def main() -> None:
    configure_tracing(service_name="pyrrhula-worker-timeouts")
    log.info("timeouts.startup")
    notifier = get_notifier()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        await sweep_due_reminders(notifier)
        await sweep_expired_awaits()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_INTERVAL_SECONDS)

    log.info("timeouts.shutdown")


if __name__ == "__main__":
    asyncio.run(main())
