"""The workspace assistant -- a required ``informational`` persona for internal tasks.

Every workspace carries exactly one assistant (key ``assistant``): it drafts persona
descriptions, drafts knowledge entries, and answers questions about the workspace's own
material. It is not a session actor -- the scheduler never gives it a turn unless a phase
explicitly asks for an informational role -- and it cannot be archived (the API refuses).

Grounding is retrieval over the workspace's knowledge, scoped by the **requesting
viewer's** entitlements (``scopes_for`` with the EXPORT pseudo-phase), not the
assistant's: who-knows-what is enforced by the system, so the assistant can never quote
material at a user that the user could not read directly (INV-4 keeps every query
scope-keyed in SQL).

Drafting calls meter as ``purpose='rewrite'`` (same taxonomy slot as the F3.12 edit
proposals they generalise); free questions meter as ``purpose='generation'``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy import select

from core.agents.models import Agent, Persona
from core.assembler.visibility import EXPORT, scopes_for
from core.audit.models import UsageRecordRow
from core.config import get_settings
from core.knowledge.retrieval.assemble import search_and_budget
from core.knowledge.retrieval.rerank import fetch_chunk_texts
from core.ports.embedding import EmbeddingProvider, EmbedRequest
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.tenancy.egress import load_egress_policy
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope

ASSISTANT_KEY = "assistant"
ASSISTANT_PERSONA_TYPE = "informational"

_ASSISTANT_PERSONA_MD = (
    "You are the workspace assistant. You help the humans running this workspace with "
    "internal tasks: drafting persona descriptions, drafting knowledge entries "
    "(rulebooks, handbooks, briefs), and answering questions about the workspace's own "
    "material. You are concise, concrete, and you ground what you write in the provided "
    "workspace knowledge when any is given."
)

# Retrieval budget for one assist call: enough grounding to matter, small enough that a
# local model's context survives alongside the instruction and the draft.
_CONTEXT_MAX_TOKENS = 2400
_CLASS_RATIOS = {"rules": 0.35, "lore": 0.40, "misc": 0.25}

_TASK_SYSTEM_PROMPTS = {
    "draft_persona": (
        "Draft a persona description (prose shown to a model as its own turn context) "
        "for the subject the user names. Write 2-4 short paragraphs in second person "
        '("You are ..."), covering role, voice, and priorities. Return only the persona '
        "text, no commentary."
    ),
    "draft_knowledge": (
        "Draft a knowledge entry (markdown) about the subject the user names, suitable "
        "for a workspace rulebook/handbook. Use headings and lists where they help. "
        "Return only the entry body, no commentary."
    ),
    "ask": (
        "Answer the user's question about this workspace using the provided workspace "
        "knowledge where relevant. Be direct; say when the knowledge does not cover "
        "something rather than inventing it."
    ),
}

_TASK_PURPOSES = {"draft_persona": "rewrite", "draft_knowledge": "rewrite", "ask": "generation"}


class UnknownAssistTaskError(Exception):
    pass


class _AssistDraft(BaseModel):
    text: str


@dataclass(frozen=True)
class AssistResult:
    text: str
    context_entry_keys: list[str] = field(default_factory=list)
    model_string: str = ""


async def get_workspace_assistant(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Persona | None:
    async with tenant_scope(tenant_id) as session:
        persona: Persona | None = await session.scalar(
            select(Persona).where(
                Persona.workspace_id == workspace_id,
                Persona.key == ASSISTANT_KEY,
                Persona.archived_at.is_(None),
            )
        )
        return persona


async def ensure_workspace_assistant(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Persona:
    """Idempotent: the workspace's assistant, created on first need. The model profile is
    found by name (one per tenant, shared across workspaces) or created from
    ``Settings.assistant_model`` -- after creation the profile is ordinary, editable
    tenant data; the setting is only the cold-start default."""
    existing = await get_workspace_assistant(tenant_id, workspace_id)
    if existing is not None:
        return existing

    settings = get_settings()
    provider_kind, _, model = settings.assistant_model.partition("/")
    async with tenant_scope(tenant_id) as session:
        profile = await session.scalar(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.name == "Assistant model",
                Agent.archived_at.is_(None),
            )
        )
        if profile is None:
            # No provider guess when nothing is configured: inventing "ollama" here is
            # what made a fresh install look like it had a working local model. An empty
            # profile is honest -- the assistant still exists (it is required), and the
            # first call that needs a model says so instead of failing at the transport.
            profile = Agent(
                tenant_id=tenant_id,
                name="Assistant model",
                provider=provider_kind if settings.assistant_model else "",
                model=(model or settings.assistant_model) if settings.assistant_model else "",
                api_base=settings.assistant_api_base or None,
            )
            session.add(profile)
            await session.flush()

        principal = Principal(tenant_id=tenant_id, kind="agent", display_name="Assistant")
        session.add(principal)
        await session.flush()

        persona = Persona(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=principal.id,
            key=ASSISTANT_KEY,
            name="Assistant",
            agent_id=profile.id,
            persona_type=ASSISTANT_PERSONA_TYPE,
            persona_md=_ASSISTANT_PERSONA_MD,
        )
        session.add(persona)
        await session.flush()
        return persona


async def _workspace_context(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    query_text: str,
    embedder: EmbeddingProvider,
) -> tuple[str, list[str]]:
    """Viewer-entitled knowledge chunks rendered as one context block. EXPORT visibility
    = "every scope this viewer is entitled to in this workspace" -- the assistant has no
    scope entitlements of its own to launder a read through."""
    scope_set = await scopes_for(tenant_id, viewer.id, workspace_id, EXPORT, None)
    if not scope_set:
        return "", []

    embedding = (await embedder.embed(EmbedRequest(model=embedder.model_name, texts=[query_text])))[
        0
    ]
    chunks = await search_and_budget(
        tenant_id,
        scope_keys=scope_set,
        query_embedding=embedding,
        query_text=query_text,
        class_ratios=_CLASS_RATIOS,
        max_tokens=_CONTEXT_MAX_TOKENS,
    )
    if not chunks:
        return "", []

    texts = await fetch_chunk_texts(tenant_id, [c.chunk_id for c in chunks])
    blocks: list[str] = []
    entry_keys: list[str] = []
    for chunk in chunks:
        body = texts.get(chunk.chunk_id, "").strip()
        if not body:
            continue
        blocks.append(f"[{chunk.entry_key}]\n{body}")
        if chunk.entry_key not in entry_keys:
            entry_keys.append(chunk.entry_key)
    return "\n\n".join(blocks), entry_keys


async def assist(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    *,
    task: str,
    subject: str,
    instruction: str,
    profile: Agent,
    provider: ModelProvider,
    embedder: EmbeddingProvider,
    api_key: str | None = None,
) -> AssistResult:
    if task not in _TASK_SYSTEM_PROMPTS:
        raise UnknownAssistTaskError(f"unknown assist task {task!r}")

    from core.usage_limits import ensure_within_limits

    persona_row = await get_workspace_assistant(tenant_id, workspace_id)
    await ensure_within_limits(
        tenant_id,
        agent_id=profile.id,
        persona_id=persona_row.id if persona_row else None,
        principal_id=viewer.id,
    )

    query_text = f"{subject}\n{instruction}".strip()
    context, entry_keys = await _workspace_context(
        tenant_id, workspace_id, viewer, query_text, embedder
    )

    user_parts: list[str] = []
    if context:
        user_parts.append(f"Workspace knowledge:\n{context}")
    if subject:
        user_parts.append(f"Subject: {subject}")
    user_parts.append(f"Request: {instruction}" if instruction else "Request: (none given)")

    model_string = f"{profile.provider}/{profile.model}"
    system_prompt = f"{_ASSISTANT_PERSONA_MD}\n\n{_TASK_SYSTEM_PROMPTS[task]}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        purpose=_TASK_PURPOSES[task],
        max_tokens=900,
        api_base=profile.api_base,
        params=dict(profile.params or {}),
        api_key=api_key,
    )

    start = time.monotonic()
    result = await provider.generate_structured(req, _AssistDraft)
    latency_ms = int((time.monotonic() - start) * 1000)

    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=profile.id,
                provider=profile.provider,
                model=profile.model,
                purpose=_TASK_PURPOSES[task],
                principal_id=viewer.id,
                prompt_tokens=sum(
                    provider.count_tokens(str(m.get("content") or ""), model_string)
                    for m in req.messages
                ),
                completion_tokens=provider.count_tokens(result.text, model_string),
                latency_ms=latency_ms,
            )
        )

    return AssistResult(
        text=result.text.strip(), context_entry_keys=entry_keys, model_string=model_string
    )
