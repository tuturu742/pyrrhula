"""Driving a session that was resumed outside an HTTP request.

Every other advance in the system starts from someone's request: a message posted, an
await satisfied through the API, a session created. A session woken because its delegated
work finished has no such caller -- the worker satisfied the await, and without this the
flow would sit one phase further on, correct and stopped.

Same composition as ``api.routes.sessions._run_process_definition_advance``, from the
worker's own factories, and the same reason for looping: ``advance_session`` stops at its
runaway guard reporting 'active', which means "nothing is blocking, there is more to do".
Returning there abandons the session mid-flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import structlog

from core.process.authoring import get_definition
from core.process.dsl.validator import validate_raw
from core.process.live_session import run_process_definition_session
from core.process.locking import (
    SessionClaimTimeoutError,
    SessionConflictError,
    claim_session,
    commit_advance,
    refresh_claim,
    release_claim,
)
from worker.embedding_provider_factory import get_embedding_provider
from worker.encryptor_factory import get_encryptor
from worker.job_queue_factory import get_job_queue
from worker.mcp_transport_factory import get_mcp_transport
from worker.model_provider_factory import get_model_provider
from worker.permission_service_factory import get_permission_service

log = structlog.get_logger()

_MAX_CONTINUATIONS = 12
# Far below the claim watchdog, so a live holder never looks silent. The watchdog then
# measures silence rather than duration, which is the thing it was always trying to ask.
_HEARTBEAT_SECONDS = 30


async def _beat(tenant_id: uuid.UUID, session_id: uuid.UUID, claimant: str) -> None:
    """Say "still here" until cancelled. A lost claim ends the loop rather than fighting
    for it back: whoever holds it now is the one advancing this session."""
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        if not await refresh_claim(tenant_id, session_id, claimant):
            log.warning("advance.claim_lost", session_id=str(session_id))
            return


async def handle_advance_session(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = uuid.UUID(str(payload["tenant_id"]))
    session_id = uuid.UUID(str(payload["session_id"]))

    from core.sessions.lifecycle import get_session

    sess = await get_session(tenant_id, session_id)
    if sess is None or sess.process_definition_id is None:
        return {"session_id": str(session_id), "advanced": False, "reason": "no definition"}
    if sess.archived_at is not None:
        # Archiving is documented as "not a pause", which is about the append-only record
        # staying intact -- it was never meant to mean the session keeps running. A queued
        # advance ran anyway, so an archived session went on spending the tenant's API
        # budget, holding entity bindings its personas needed for the next session, and
        # competing for a worker, none of it visible: the whole point of archiving is that
        # it drops out of the session list, so nobody can see it still going.
        log.info("advance.archived_session", session_id=str(session_id))
        return {"session_id": str(session_id), "advanced": False, "reason": "archived"}

    definition_row = await get_definition(tenant_id, sess.process_definition_id)
    if definition_row is None:
        return {"session_id": str(session_id), "advanced": False, "reason": "definition gone"}
    dsl, issues = validate_raw(definition_row.definition)
    if dsl is None or issues:
        return {"session_id": str(session_id), "advanced": False, "reason": "definition invalid"}

    steps = 0
    status = sess.status
    claimant = f"worker:{uuid.uuid4().hex[:8]}"
    try:
        async with claim_session(tenant_id, session_id, claimant) as observed_version:
            heartbeat = asyncio.create_task(_beat(tenant_id, session_id, claimant))
            try:
                for _ in range(_MAX_CONTINUATIONS):
                    result = await run_process_definition_session(
                        tenant_id,
                        session_id,
                        dsl,
                        model_provider_factory=get_model_provider,
                        embedding_provider=get_embedding_provider(),
                        encryptor=get_encryptor(),
                        permission_service=get_permission_service(),
                        mcp_transport=get_mcp_transport(),
                        job_queue=get_job_queue(),
                    )
                    steps += result.steps_taken
                    status = result.status
                    # Any status but 'active' is a real stop, and 'active' with no
                    # progress would spin.
                    if result.status != "active" or result.steps_taken == 0:
                        break
            except Exception:
                # Release without bumping the version, so a failed attempt does not hold
                # the claim for the whole watchdog window before anyone may retry.
                await release_claim(tenant_id, session_id)
                raise
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
    except SessionClaimTimeoutError as exc:
        # Not an error: another worker has this session. Say so and let it finish --
        # whatever enqueued this will enqueue again when there is more to do.
        log.info("advance.already_claimed", session_id=str(session_id), detail=str(exc)[:160])
        return {"session_id": str(session_id), "advanced": False, "reason": "claimed elsewhere"}

    with contextlib.suppress(SessionConflictError):
        # The claim is exclusive, so a version move here should be impossible; treat it
        # as someone else having legitimately committed rather than failing a turn that
        # already happened.
        await commit_advance(tenant_id, session_id, observed_version)

    log.info(
        "advance.resumed_session",
        session_id=str(session_id),
        steps=steps,
        status=status,
    )
    if status == "terminal":
        # A finished session has no reworks left to keep a warm container for. Until
        # this, only archiving tore them down, and a host that ran a sample twice kept
        # every delegation container of both runs (sweep 2026-10-04).
        await get_job_queue().enqueue(
            tenant_id,
            "teardown_session_envs",
            {"session_id": str(session_id), "tenant_id": str(tenant_id)},
        )
    return {"session_id": str(session_id), "advanced": True, "steps": steps, "status": status}
