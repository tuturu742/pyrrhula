"""Capability enforcement: assigning a `behavior_profile` with a
high-stakes axis to an agent whose model fails that axis's capability check is a
validation error, not a silent downgrade -- a behavioural parameter a model can't
actually honor is worse than an absent one; it gives the author false confidence.
"""

from __future__ import annotations

import uuid

from core.agents.models import Agent, Persona
from core.behavior.capabilities import get_capability
from core.behavior.repo import get_axis_definition
from core.tenancy.scope import tenant_scope


class CapabilityRejectedError(Exception):
    pass


async def validate_profile_capability(
    tenant_id: uuid.UUID, persona_id: uuid.UUID, pack_id: str, axis_values: dict[str, int]
) -> None:
    """Only `stakes: high` axes are enforced -- a low-stakes axis (`prompt_directive`
    only) degrading on an incapable model is a style-quality concern (the
    `behavioral_fidelity` metric already tracks it), not a correctness one, so it never
    blocks assignment the way a high-stakes axis does."""
    async with tenant_scope(tenant_id) as session:
        persona = await session.get(Persona, persona_id)
        if persona is None:
            return
        connection = await session.get(Agent, persona.agent_id)
        if connection is None:
            return
        provider, model = connection.provider, connection.model

    for axis_key in axis_values:
        axis = await get_axis_definition(tenant_id, pack_id, axis_key)
        if axis is None or axis.stakes != "high":
            continue
        capability = await get_capability(provider, model, axis_key)
        if capability is not None and not capability.capable:
            raise CapabilityRejectedError(
                f"model {provider}/{model} fails capability for high-stakes axis "
                f"{axis_key!r}: {capability.reason or 'below fidelity threshold'}"
            )
