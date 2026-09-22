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

import uuid
from typing import Any

import structlog

from core.process.authoring import get_definition
from core.process.dsl.validator import validate_raw
from core.process.live_session import run_process_definition_session
from worker.embedding_provider_factory import get_embedding_provider
from worker.encryptor_factory import get_encryptor
from worker.job_queue_factory import get_job_queue
from worker.mcp_transport_factory import get_mcp_transport
from worker.model_provider_factory import get_model_provider
from worker.permission_service_factory import get_permission_service

log = structlog.get_logger()

_MAX_CONTINUATIONS = 12


async def handle_advance_session(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = uuid.UUID(str(payload["tenant_id"]))
    session_id = uuid.UUID(str(payload["session_id"]))

    from core.sessions.lifecycle import get_session

    sess = await get_session(tenant_id, session_id)
    if sess is None or sess.process_definition_id is None:
        return {"session_id": str(session_id), "advanced": False, "reason": "no definition"}

    definition_row = await get_definition(tenant_id, sess.process_definition_id)
    if definition_row is None:
        return {"session_id": str(session_id), "advanced": False, "reason": "definition gone"}
    dsl, issues = validate_raw(definition_row.definition)
    if dsl is None or issues:
        return {"session_id": str(session_id), "advanced": False, "reason": "definition invalid"}

    steps = 0
    status = sess.status
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
        # Any status but 'active' is a real stop, and 'active' with no progress would
        # spin.
        if result.status != "active" or result.steps_taken == 0:
            break

    log.info(
        "advance.resumed_session",
        session_id=str(session_id),
        steps=steps,
        status=status,
    )
    return {"session_id": str(session_id), "advanced": True, "steps": steps, "status": status}
