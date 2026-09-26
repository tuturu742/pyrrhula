"""the real ``persona_candidate_resolver`` implementation
``core.process.scheduler.make_default_candidate_resolver`` has an injection seam for
exactly this -- ``Persona.persona_type`` is what this resolves against.

``order == "initiative"`` with no explicit selector (the DSL's "implicit eligibility:
whoever has the referenced entity field" shape) resolves to an empty candidate list,
not an error -- this resolver does not query entities. A phase actually relying on this
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
    with_chattiness: bool = False,
) -> list[Candidate]:
    """Personas of ``persona_type`` eligible to act. When ``session_id`` names a session with
    an explicit roster (#4), candidates come from that roster; otherwise (skeleton/legacy
    sessions with no roster) they fall back to the whole workspace. Archived personas never
    qualify either way."""
    async with tenant_scope(tenant_id) as session:
        stmt = select(Persona.id, Persona.principal_id, Persona.name).where(
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
        rows = (await session.execute(stmt.order_by(Persona.principal_id))).all()

    if not with_chattiness:
        return [Candidate(principal_id=pid, name=name) for _persona_id, pid, name in rows]

    # Each candidate's chattiness axis value, when its behaviour profile sets one --
    # "reactive" ordering weights unprompted turns by it, and is the only mode that does,
    # so the per-persona profile reads happen only when a phase actually asked for them.
    # Personas with no profile (or one that never set the axis) ride the neutral default.
    from core.behavior.repo import get_current_behavior_profile

    candidates: list[Candidate] = []
    for persona_id, pid, name in rows:
        profile = await get_current_behavior_profile(tenant_id, persona_id)
        raw = (profile.axis_values or {}).get("chattiness") if profile is not None else None
        chattiness = float(raw) if isinstance(raw, (int, float)) else None
        candidates.append(Candidate(principal_id=pid, name=name, chattiness=chattiness))
    return candidates


def make_persona_candidate_resolver(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> CandidateResolver:
    """Pass the result as ``make_default_candidate_resolver(workspace_id,
    persona_candidate_resolver=...)``'s ``persona_candidate_resolver`` argument."""

    async def resolve(spec: ActorSpec, ctx: InterpreterContext) -> list[Candidate]:
        session_id = ctx.session_id
        if spec.persona_type is not None:
            return await _personas_with_type(
                tenant_id,
                workspace_id,
                spec.persona_type,
                session_id=session_id,
                with_chattiness=spec.order == "reactive",
            )

        if spec.any_of is not None:
            # In the order the author wrote them, not alphabetically. `order: "declared"`
            # promises the declared order, and a set + sorted() threw it away twice over:
            # any_of ["supervisor_agent", "participant_agent"] scheduled every participant
            # ahead of the supervisor, so a phase meant to be led by its facilitator opened
            # with whichever suspect sorted first. Dedupe keeping first appearance.
            roles: list[str] = []
            for token in spec.any_of:
                if not token.endswith(_AGENT_TOKEN_SUFFIX):
                    continue
                role_token = token[: -len(_AGENT_TOKEN_SUFFIX)]
                if role_token not in roles:
                    roles.append(role_token)
            candidates: list[Candidate] = []
            for role in roles:
                candidates.extend(
                    await _personas_with_type(
                        tenant_id,
                        workspace_id,
                        role,
                        session_id=session_id,
                        with_chattiness=spec.order == "reactive",
                    )
                )
            return candidates

        # order == "initiative" with no explicit selector -- see module docstring.
        return []

    return resolve
