"""Disclosure decision + event persistence. Split out of
`core.secrets.repo` (INV-1-restricted) because writing one of these rows never reads
`secret.content` — only ids and already-computed gist-based judgments the caller already
has in hand. Freely importable: `core.secrets.gate` persists its own output
directly through this module, since it cannot import `core.secrets.repo` itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from core.audit.models import UsageRecordRow
from core.secrets.models import DisclosureDecisionRow, SecretDisclosureEventRow, SecretHolderRow
from core.tenancy.scope import tenant_scope


async def record_gate_decision(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    persona_id: uuid.UUID,
    behavior_profile_version: int,
    decisions: list[dict[str, object]],
    *,
    agent_id: uuid.UUID,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    workspace_id: uuid.UUID | None = None,
) -> DisclosureDecisionRow:
    """The gate's rationale  — evidence, not the record of what was actually said
    (that's `record_disclosure_event`, written separately once a decision is acted on).
    Writes the `DisclosureDecisionRow` and its `usage_record` (`purpose='gate'`) in the
    SAME transaction (CLAUDE.md rule 11) — a gate call that ran and cost money but whose
    metering silently didn't commit is exactly the "drift between what happened and what
    was billed" this rule exists to prevent."""
    async with tenant_scope(tenant_id) as session:
        decision_row = DisclosureDecisionRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            persona_id=persona_id,
            behavior_profile_version=behavior_profile_version,
            decisions=decisions,
            agent_id=agent_id,
            latency_ms=latency_ms,
            token_usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        )
        session.add(decision_row)
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                persona_id=persona_id,
                agent_id=agent_id,
                provider=provider,
                model=model,
                purpose="gate",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
        )
        await session.flush()
        await session.refresh(decision_row)
        return decision_row


async def record_disclosure_event(
    tenant_id: uuid.UUID,
    secret_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    mode: str,
    disclosed_to: dict[str, object],
    *,
    disclosed_by_principal_id: uuid.UUID | None = None,
    decision_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
) -> SecretDisclosureEventRow:
    async with tenant_scope(tenant_id) as session:
        row = SecretDisclosureEventRow(
            tenant_id=tenant_id,
            secret_id=secret_id,
            session_id=session_id,
            event_seq=event_seq,
            disclosed_by_principal_id=disclosed_by_principal_id,
            disclosed_to=disclosed_to,
            mode=mode,
            decision_id=decision_id,
            message_id=message_id,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def record_reveal(
    tenant_id: uuid.UUID,
    secret_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    decision_id: uuid.UUID | None,
    *,
    disclosed_by_principal_id: uuid.UUID | None,
    new_holder_principal_ids: Sequence[uuid.UUID],
    message_id: uuid.UUID | None = None,
) -> SecretDisclosureEventRow:
    """The reveal path: the event and every new holder in ONE transaction (CLAUDE.md
    rule 4's tenant_scope() already gives per-call atomicity; the point here is doing
    *both* writes inside that one call, not two separate ones) -- a forced failure
    partway (e.g. a holder id that doesn't exist) rolls back the event too, so the world
    never ends up in a "disclosure happened but nobody was actually told" state or vice
    versa. Idempotent per holder: a principal already holding the secret isn't re-added
    or duplicated (`uq_secret_holder`)."""
    async with tenant_scope(tenant_id) as session:
        event_row = SecretDisclosureEventRow(
            tenant_id=tenant_id,
            secret_id=secret_id,
            session_id=session_id,
            event_seq=event_seq,
            disclosed_by_principal_id=disclosed_by_principal_id,
            disclosed_to={"principal_ids": [str(p) for p in new_holder_principal_ids]},
            mode="full",
            decision_id=decision_id,
            message_id=message_id,
        )
        session.add(event_row)
        await session.flush()

        for holder_principal_id in new_holder_principal_ids:
            existing = await session.scalar(
                select(SecretHolderRow.id).where(
                    SecretHolderRow.secret_id == secret_id,
                    SecretHolderRow.holder_principal_id == holder_principal_id,
                )
            )
            if existing is None:
                session.add(
                    SecretHolderRow(
                        tenant_id=tenant_id,
                        secret_id=secret_id,
                        holder_principal_id=holder_principal_id,
                        holder_kind="told",
                        acquired_via_event_id=None,
                    )
                )
        await session.flush()
        await session.refresh(event_row)
        return event_row
