"""chat-based editing for an agent's persona (req 22) -- the third of
this task's three proposal targets. Same draft-and-approve shape as
``core.knowledge.editing``/``core.entities.editing``: propose a full replacement
``persona_md``, diff it against the current text, meter the call regardless of outcome,
and only write (through ``core.agents.authoring.record_persona_version``) once a human
approves. Personas have no schema-shaped validation the way entity schemas do (prose has
no CEL to compile-check), so "validation" here is limited to non-emptiness and the same
plaintext-leak guardrail the other two targets reuse.
"""

from __future__ import annotations

import difflib
import time
import uuid
from dataclasses import dataclass

from pydantic import BaseModel

from core.agents.authoring import PersonaNotFoundError, get_persona, record_persona_version
from core.agents.models import Agent, Persona
from core.audit.models import UsageRecordRow
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope

_PURPOSE = "rewrite"

_SYSTEM_PROMPT = (
    "You edit an agent's persona -- a short prose description shown to the model as part "
    "of its own turn context -- given an editing instruction. Return only the full "
    "replacement persona text, never commentary."
)


class PersonaEditResult(BaseModel):
    persona_md: str


@dataclass(frozen=True)
class PersonaEditProposal:
    persona_id: uuid.UUID
    current_persona_md: str
    proposed_persona_md: str
    text_diff: str
    valid: bool
    issues: list[str]


def _diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="persona@current",
            tofile="persona@proposed",
            lineterm="",
        )
    )


async def propose_persona_edit(
    tenant_id: uuid.UUID,
    persona_id: uuid.UUID,
    instruction: str,
    *,
    agent: Agent,
    provider: ModelProvider,
    workspace_id: uuid.UUID | None = None,
    api_key: str | None = None,
) -> PersonaEditProposal:
    persona = await get_persona(tenant_id, persona_id)
    if persona is None:
        raise PersonaNotFoundError(f"no persona {persona_id}")

    model_string = f"{agent.provider}/{agent.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Current persona:\n{persona.persona_md}\n\nInstruction: {instruction}",
            },
        ],
        purpose=_PURPOSE,
        max_tokens=500,
        api_base=agent.api_base,
        params=dict(agent.params or {}),
        api_key=api_key,
    )

    start = time.monotonic()
    result = await provider.generate_structured(req, PersonaEditResult)
    latency_ms = int((time.monotonic() - start) * 1000)

    prompt_tokens = sum(
        provider.count_tokens(str(m.get("content") or ""), model_string) for m in req.messages
    )
    completion_tokens = provider.count_tokens(result.persona_md, model_string)

    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent.id,
                provider=agent.provider,
                model=agent.model,
                purpose=_PURPOSE,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
        )

    issues: list[str] = []
    if not result.persona_md.strip():
        issues.append("proposed persona is empty")

    return PersonaEditProposal(
        persona_id=persona_id,
        current_persona_md=persona.persona_md,
        proposed_persona_md=result.persona_md,
        text_diff=_diff(persona.persona_md, result.persona_md),
        valid=not issues,
        issues=issues,
    )


async def apply_persona_edit_proposal(
    tenant_id: uuid.UUID,
    persona_id: uuid.UUID,
    proposed_persona_md: str,
    *,
    approved_by: uuid.UUID,
) -> Persona:
    return await record_persona_version(
        tenant_id, persona_id, proposed_persona_md, created_by=approved_by, ai_assisted=True
    )
