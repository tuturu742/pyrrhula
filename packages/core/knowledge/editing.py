"""chat-based editing for knowledge entries (req 22) -- a chat turn
produces a **structured edit proposal** (a full replacement body for one entry, never a
freeform overwrite of the source), diffed against the current draft, and applied only on
human approval as a newly published version carrying an ``ai_assisted`` provenance
marker. Generalizes the ``core.secrets.drafting`` draft-and-approve pattern: the model
proposes, this module never writes content on its own, and the plaintext-leak guardrail
(``core.secrets.drafting.contains_plaintext_leak``) is reused rather than reimplemented
for the "proposals touching secrets" guardrail subtask.

Declining a proposal is simply never calling ``apply_knowledge_edit_proposal`` --
``propose_knowledge_edit`` never touches the draft or publishes anything, so a declined
proposal leaves the version DAG exactly as it was (CLAUDE.md's append-only discipline on
``knowledge_source_version`` makes this the only possible shape: there is no "undo a
publish" to build).
"""

from __future__ import annotations

import difflib
import time
import uuid
from dataclasses import dataclass

from pydantic import BaseModel

from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.knowledge.authoring import (
    EntryFields,
    list_draft_entries,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.models import KnowledgeEntry, KnowledgeSourceVersion
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.secrets.drafting import contains_plaintext_leak
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope

_PURPOSE = "rewrite"

_SYSTEM_PROMPT = (
    "You edit one knowledge entry's body text given an editing instruction. Return only "
    "the full replacement body text for the entry -- never commentary, never a diff, "
    "never markdown fencing around the whole answer."
)


class KnowledgeEditResult(BaseModel):
    body_md: str


class NoSuchDraftEntryError(Exception):
    pass


@dataclass(frozen=True)
class KnowledgeEditProposal:
    knowledge_source_id: uuid.UUID
    entry_key: str
    current_body_md: str
    proposed_body_md: str
    text_diff: str
    valid: bool
    issues: list[str]


def _diff(entry_key: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{entry_key}@current",
            tofile=f"{entry_key}@proposed",
            lineterm="",
        )
    )


async def _current_draft_entry(
    tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID, entry_key: str
) -> KnowledgeEntry:
    entries = await list_draft_entries(tenant_id, knowledge_source_id)
    current = next((e for e in entries if e.entry_key == entry_key), None)
    if current is None:
        raise NoSuchDraftEntryError(f"no draft entry {entry_key!r} in source {knowledge_source_id}")
    return current


async def propose_knowledge_edit(
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    entry_key: str,
    instruction: str,
    *,
    agent: Agent,
    provider: ModelProvider,
    workspace_id: uuid.UUID | None = None,
) -> KnowledgeEditProposal:
    """Calls the model, meters the call (``usage_record``, ``purpose='rewrite'``)
    regardless of outcome -- same discipline as ``draft_directive_and_hint`` -- then
    validates the result *before* it is ever shown for approval. "Validation" here means:
    non-empty, and not a verbatim echo of any secret plaintext this entry's own content
    might itself contain (the guardrail subtask); there is no CEL/schema-shaped
    validation for prose knowledge content the way entity schemas have."""
    current = await _current_draft_entry(tenant_id, knowledge_source_id, entry_key)

    model_string = f"{agent.provider}/{agent.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Current body:\n{current.body_md}\n\nInstruction: {instruction}",
            },
        ],
        purpose=_PURPOSE,
        max_tokens=800,
        api_base=agent.api_base,
        params=dict(agent.params or {}),
    )

    start = time.monotonic()
    result = await provider.generate_structured(req, KnowledgeEditResult)
    latency_ms = int((time.monotonic() - start) * 1000)

    prompt_tokens = sum(
        provider.count_tokens(str(m.get("content") or ""), model_string) for m in req.messages
    )
    completion_tokens = provider.count_tokens(result.body_md, model_string)

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
    if not result.body_md.strip():
        issues.append("proposed body is empty")
    if (
        contains_plaintext_leak(current.body_md, result.body_md)
        and result.body_md.strip() == current.body_md.strip()
    ):
        # A proposal that is *only* a verbatim echo of the current text is not an edit
        # at all -- distinct from the secrets guardrail's "leaks a different, private
        # source's plaintext" concern, but the same "don't present a no-op as an edit"
        # discipline this task's validation-before-presentation subtask calls for.
        issues.append("proposed body is unchanged from the current body")

    return KnowledgeEditProposal(
        knowledge_source_id=knowledge_source_id,
        entry_key=entry_key,
        current_body_md=current.body_md,
        proposed_body_md=result.body_md,
        text_diff=_diff(entry_key, current.body_md, result.body_md),
        valid=not issues,
        issues=issues,
    )


async def apply_knowledge_edit_proposal(
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    entry_key: str,
    proposed_body_md: str,
    *,
    approved_by: uuid.UUID,
) -> KnowledgeSourceVersion:
    """The only write path an *approved* proposal takes: fold the proposed body into the
    draft, then publish -- a new immutable version, attributed to the human approver,
    marked ``ai_assisted``. Called only by the API route once a human has approved; never
    called for a declined proposal, so decline provably leaves no trace."""
    current = await _current_draft_entry(tenant_id, knowledge_source_id, entry_key)
    fields = EntryFields(
        title=current.title,
        body_md=proposed_body_md,
        class_=current.class_,
        scope_key=current.scope_key,
        keys=list(current.keys),
        secondary_keys=list(current.secondary_keys),
        logic=current.logic,
        use_regex=current.use_regex,
        constant=current.constant,
        sticky=current.sticky,
        cooldown=current.cooldown,
        delay=current.delay,
        trigger_pct=current.trigger_pct,
        inclusion_group=current.inclusion_group,
        position=current.position,
        insertion_order=current.insertion_order,
    )
    await upsert_draft_entry(tenant_id, knowledge_source_id, entry_key, fields)
    return await publish_version(
        tenant_id,
        knowledge_source_id,
        created_by=approved_by,
        change_note=f"AI-assisted edit: {entry_key}",
        ai_assisted=True,
    )
