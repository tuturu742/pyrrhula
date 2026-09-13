"""B1.8: the real ``persona_candidate_resolver`` implementation
``core.process.scheduler.make_default_candidate_resolver`` has had an injection seam
for since B1.3 -- ``Persona.persona_type`` (added at B1.7) is exactly what this resolves
against; nothing wired it into the scheduler until now.

``order == "initiative"`` with no explicit selector (the DSL's "implicit eligibility:
whoever has the referenced entity field" shape) resolves to an empty candidate list,
not an error -- there is no Entity system anywhere in Phase 1 (a gap B1.3/B1.4 already
found and documented), so there is nothing to query. A phase actually relying on this
shape simply never gets a turn from this resolver; that's a real, honest limitation,
not a silent wrong answer.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.agents.models import Persona
from core.process.dsl.schema import ActorSpec
from core.process.interpreter import InterpreterContext
from core.process.scheduler import Candidate, CandidateResolver
from core.sessions.models import SessionPersonaRow
from core.tenancy.scope import tenant_scope

_AGENT_TOKEN_SUFFIX = "_agent"


async def _session_has_roster(tenant_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    async with tenant_scope(tenant_id) as session:
        found = await session.scalar(
            select(SessionPersonaRow.id).where(SessionPersonaRow.session_id == session_id).limit(1)
        )
        return found is not None


async def _personas_with_type(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    persona_type: str,
    *,
    session_id: uuid.UUID | None = None,
) -> list[Candidate]:
    """Personas of ``persona_type`` eligible to act. When ``session_id`` names a session with
    an explicit roster (#4), candidates come from that roster; otherwise (skeleton/legacy
    sessions with no roster) they fall back to the whole workspace. Archived personas never
    qualify either way."""
    async with tenant_scope(tenant_id) as session:
        stmt = select(Persona.principal_id).where(
            Persona.tenant_id == tenant_id,
            Persona.persona_type == persona_type,
            Persona.archived_at.is_(None),
        )
        if session_id is not None and await _session_has_roster(tenant_id, session_id):
            stmt = stmt.join(SessionPersonaRow, SessionPersonaRow.persona_id == Persona.id).where(
                SessionPersonaRow.session_id == session_id
            )
        else:
            stmt = stmt.where(Persona.workspace_id == workspace_id)
        rows = (await session.execute(stmt.order_by(Persona.principal_id))).scalars()
        return [Candidate(principal_id=pid) for pid in rows]


def make_persona_candidate_resolver(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> CandidateResolver:
    """Pass the result as ``make_default_candidate_resolver(workspace_id,
    persona_candidate_resolver=...)``'s ``persona_candidate_resolver`` argument."""

    async def resolve(spec: ActorSpec, ctx: InterpreterContext) -> list[Candidate]:
        session_id = ctx.session_id
        if spec.persona_type is not None:
            return await _personas_with_type(
                tenant_id, workspace_id, spec.persona_type, session_id=session_id
            )

        if spec.any_of is not None:
            roles = {
                token[: -len(_AGENT_TOKEN_SUFFIX)]
                for token in spec.any_of
                if token.endswith(_AGENT_TOKEN_SUFFIX)
            }
            candidates: list[Candidate] = []
            for role in sorted(roles):
                candidates.extend(
                    await _personas_with_type(tenant_id, workspace_id, role, session_id=session_id)
                )
            return candidates

        # order == "initiative" with no explicit selector -- see module docstring.
        return []

    return resolve
