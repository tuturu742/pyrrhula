"""The two moderation scans names (G4.14, req 31).

**Authoring** (`scan_authored`) -- secrets, knowledge entries, and personas at write time.
It reads a secret's `content` regardless of `disclosure_state`, deliberately: concealment
governs what reaches a *model*, and a scanner that honoured it would be a scanner the
secrets system blinded. That is the whole point of calling this a DB-connected scan.

**Generation** (`scan_generated`) -- replies before delivery, following the ladder
exactly: regenerate once, then a safe fallback, then an overseer alert where an overseer
exists. Not a new ladder; the same one, because a leak and a policy violation have the same
shape from a reader's point of view (something reached them that shouldn't have) and two
different recovery behaviours would be two things to reason about.

**Audit records the decision, never the content.** Category, action, and target id. A
moderation log full of the text it flagged is a second copy of every sensitive thing
anyone wrote, sitting in a table with different access rules from the original.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from core.audit.service import AuditService
from core.moderation.policy import ModerationPolicy, ScanOutcome, get_policy
from core.ports.moderation import ModerationProvider
from core.sessions.models import SessionEventRow
from core.tenancy.scope import tenant_scope

RegenerateFn = Callable[[], Awaitable[str]]

FALLBACK_REPLY = "I'd rather not go into that."

_AUDIT_ACTION = "moderation:scan"


async def scan_authored(
    tenant_id: uuid.UUID,
    text: str,
    *,
    context: str,
    target_type: str,
    target_id: uuid.UUID | None,
    actor_principal_id: uuid.UUID,
    provider: ModerationProvider,
    audit_service: AuditService | None = None,
    policy: ModerationPolicy | None = None,
) -> ScanOutcome:
    """The authoring hook. ``context`` is the provider's own hint (`secret`, `knowledge`,
    `persona`); ``target_type``/``target_id`` are what the audit row names.

    Returns rather than raises even when the action is `block`: the caller is the authoring
    service, and it is better placed to turn "blocked" into its own domain error (a refused
    save, a rejected import) than this module is to guess which."""
    resolved = policy or await get_policy(tenant_id)
    if not resolved.enabled:
        return ScanOutcome(allowed=True, action_taken="skipped")

    result = await provider.check(text, context=context)
    outcome = _decide(resolved, result.allowed, tuple(result.reasons))
    await _audit(
        tenant_id,
        actor_principal_id,
        target_type,
        target_id,
        outcome,
        audit_service=audit_service,
    )
    return outcome


async def scan_generated(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    reply_text: str,
    *,
    actor_principal_id: uuid.UUID,
    provider: ModerationProvider,
    regenerate: RegenerateFn,
    audit_service: AuditService | None = None,
    policy: ModerationPolicy | None = None,
) -> tuple[str, ScanOutcome]:
    """The generation hook, following the ladder. Returns ``(final_text, outcome)``.

    ``regenerate`` is called **at most once**, whatever happens. An agent that keeps
    producing blocked content gets the fallback and an alert, never a third attempt --
    exactly as E2.7 reasons about a concealed agent that keeps leaking."""
    resolved = policy or await get_policy(tenant_id)
    if not resolved.enabled:
        return reply_text, ScanOutcome(allowed=True, action_taken="skipped")

    first = await provider.check(reply_text, context="generation")
    if first.allowed:
        return reply_text, ScanOutcome(allowed=True, action_taken="clean")

    if not resolved.blocks:
        # `flag`/`queue` do not withhold the reply -- they mark it. Withholding on a policy
        # that did not ask to withhold would be the module deciding a tenant's posture for
        # them.
        outcome = _decide(resolved, allowed=False, reasons=tuple(first.reasons))
        await _audit(
            tenant_id,
            actor_principal_id,
            "message",
            None,
            outcome,
            audit_service=audit_service,
        )
        return reply_text, outcome

    regenerated = await regenerate()
    second = await provider.check(regenerated, context="generation")
    if second.allowed:
        outcome = ScanOutcome(
            allowed=True, action_taken="regenerated", reasons=tuple(first.reasons)
        )
        await _audit(
            tenant_id,
            actor_principal_id,
            "message",
            None,
            outcome,
            audit_service=audit_service,
        )
        return regenerated, outcome

    outcome = ScanOutcome(allowed=False, action_taken="fallback", reasons=tuple(second.reasons))
    await _write_overseer_alert(tenant_id, session_id, event_seq, outcome)
    await _audit(
        tenant_id, actor_principal_id, "message", None, outcome, audit_service=audit_service
    )
    return FALLBACK_REPLY, outcome


def _decide(policy: ModerationPolicy, allowed: bool, reasons: tuple[str, ...]) -> ScanOutcome:
    if allowed:
        return ScanOutcome(allowed=True, action_taken="clean")
    if policy.action == "block":
        return ScanOutcome(allowed=False, action_taken="block", reasons=reasons)
    # `flag` and `queue` both let the content through; they differ in what a human is
    # expected to do next, which is a UI distinction rather than a gate one.
    return ScanOutcome(allowed=True, action_taken=policy.action, reasons=reasons)


async def _audit(
    tenant_id: uuid.UUID,
    actor_principal_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID | None,
    outcome: ScanOutcome,
    *,
    audit_service: AuditService | None,
) -> None:
    await (audit_service or AuditService()).append(
        tenant_id=tenant_id,
        actor_principal_id=actor_principal_id,
        action=_AUDIT_ACTION,
        resource_type=target_type,
        resource_id=target_id,
        query={"action_taken": outcome.action_taken, "categories": list(outcome.reasons)},
    )


async def _write_overseer_alert(
    tenant_id: uuid.UUID, session_id: uuid.UUID, event_seq: int, outcome: ScanOutcome
) -> None:
    """The same `session_event` stream the leak alert uses, with a distinct kind. One
    attention feed for the overseer, whether the near-miss was a leak or a policy
    violation -- two feeds is one feed nobody checks."""
    async with tenant_scope(tenant_id) as session:
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="moderation_alert",
                payload={"action_taken": outcome.action_taken, "categories": list(outcome.reasons)},
            )
        )
