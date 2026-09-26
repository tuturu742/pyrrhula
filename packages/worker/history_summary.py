"""History-summarisation job handler -- the worker-side wiring
``core.sessions.history`` needs but can't import itself (composition root: which
``ModelProvider`` and which ``PermissionService`` adapter, the same rule
``worker.ingestion`` follows). Registered in ``worker.main``'s dispatch table under kind
``"summarise_history"``.

Off the request path on purpose: a month of elapsed history is a map-reduce over the whole
session log with one model call per phase, which is not something a human clicking
"resume" should sit through. The job writes nothing but its ``usage_record`` rows -- the
summary itself is returned to the caller, who decides which turn it gets injected into and
records that decision on that turn's manifest (``context_manifest.history_summary_hash``).

Idempotency (rule 8) is keyed on the exact summarisation request -- the same session, the
same viewer, the same event range, the same budget produce the same summary, so a retried
job returns the cached result instead of paying for the map-reduce twice.
"""

from __future__ import annotations

import uuid
from typing import Any

from adapters.permission.role_permission import RolePermissionService
from core.actions.idempotency import idempotent
from core.agents.models import Agent
from core.process.dsl.schema import PhaseSpec
from core.sessions.history import summarise_history
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from worker.model_provider_factory import get_model_provider


def _summary_job_key(**kwargs: Any) -> str:
    return (
        f"summarise_history:{kwargs['session_id']}:{kwargs['viewer_principal_id']}:"
        f"{kwargs['from_event_seq']}-{kwargs['to_event_seq']}:{kwargs['max_tokens']}"
    )


@idempotent(key_fn=_summary_job_key)
async def run_history_summary_job(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer_principal_id: uuid.UUID,
    agent_id: uuid.UUID,
    phase: dict[str, Any],
    from_event_seq: int,
    to_event_seq: int,
    max_tokens: int,
) -> dict[str, Any]:
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, viewer_principal_id)
        if viewer is None:
            raise ValueError(f"no principal {viewer_principal_id} in this tenant")
        agent = await session.get(Agent, agent_id)
        if agent is None:
            raise ValueError(f"no agent {agent_id} in this tenant")
        session.expunge(viewer)
        session.expunge(agent)

    summary = await summarise_history(
        tenant_id,
        workspace_id,
        session_id,
        viewer,
        PhaseSpec.model_validate(phase),
        from_event_seq=from_event_seq,
        to_event_seq=to_event_seq,
        max_tokens=max_tokens,
        agent=agent,
        provider=get_model_provider(agent.provider),
        permission_service=RolePermissionService(),
    )
    return {
        "rendered_text": summary.rendered_text,
        "token_count": summary.token_count,
        "content_hash": summary.content_hash,
        "from_event_seq": summary.from_event_seq,
        "to_event_seq": summary.to_event_seq,
        "fact_count": len(summary.facts),
    }


async def handle_summarise_history(payload: dict[str, Any]) -> dict[str, Any]:
    """Job-payload adapter: ``JobQueue.Job.payload`` is a plain JSON dict (ids arrive as
    strings, the phase as its serialised DSL document)."""
    return await run_history_summary_job(
        tenant_id=uuid.UUID(payload["tenant_id"]),
        workspace_id=uuid.UUID(payload["workspace_id"]),
        session_id=uuid.UUID(payload["session_id"]),
        viewer_principal_id=uuid.UUID(payload["viewer_principal_id"]),
        agent_id=uuid.UUID(payload["agent_id"]),
        phase=payload["phase"],
        from_event_seq=int(payload["from_event_seq"]),
        to_event_seq=int(payload["to_event_seq"]),
        max_tokens=int(payload["max_tokens"]),
    )
