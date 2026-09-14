"""Human-in-place-of-agent (G4.4, plan §5.2 ``generate_as``, §12.7, req 10).

The director speaking as the innkeeper and the manager stepping in for a stuck engineer
agent are the same mechanism: a permitted human writes a turn that the session records as
*that agent's* turn. Two modes -- verbatim, or through an AI voice-conformance rewrite the
human approves before it posts.

Three properties this module exists to guarantee, each with a test:

**The override is never hidden.** ``was_human_override`` is written by the only function
that can write an override message, so there is no path that produces one with the flag
unset. ``overridden_by_principal_id`` names the human. The overseer always sees both; a
workspace setting decides whether participants do, defaulting to *yes* -- the honest
default, chosen once here rather than left to each UI.

**A draft is not a capability.** ``draft_override`` and ``post_override`` each check
``agent:speak_as`` independently. A permission revoked between drafting and confirming
takes effect, which it would not if the second call trusted the first.

**Downstream integrity is unconditional.** An override goes through the same leak check
(E2.7) as a generated reply: a human can leak a concealed secret's plaintext by accident
just as readily as a model can, and the tripwire does not care which one typed it.

*But the response ladder differs, deliberately.* E2.7's generated-reply ladder is
regenerate-once-then-fallback; there is nothing to regenerate here, because the words are
a person's. A leaking override is **refused** -- the message is never written, the human is
told, and the overseer is alerted. Substituting a bland fallback for what a human typed
and posting it under an agent's name would be a second, worse override on top of the
first.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select

from core.agents.models import Agent, Persona
from core.audit.models import UsageRecordRow
from core.ports.embedding import EmbeddingProvider
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.ports.permission import PermissionService
from core.secrets.leak_check import ConcealedSecret, check_for_leak
from core.sessions.lifecycle import resolve_author_name
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.egress import load_egress_policy
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope

_SPEAK_AS_ACTION = "agent:speak_as"
_REWRITE_PURPOSE = "rewrite"
_BADGE_SETTING = "show_override_badge_to_participants"

_REWRITE_SYSTEM_PROMPT = (
    "You rewrite one message so it sounds like the given character speaking, without "
    "changing what it says. Keep every fact, name, number, decision, and refusal exactly "
    "as written. Change only voice, register, and phrasing. Return only the rewritten "
    "message."
)

OverrideMode = Literal["verbatim", "voice"]


class SpeakAsDeniedError(Exception):
    """The principal holds no ``agent:speak_as`` in this workspace."""


class OverrideLeakError(Exception):
    """The override text would disclose a concealed secret. The message is not written --
    see the module docstring for why refusal, not fallback substitution, is the response."""

    def __init__(self, leaked_secret_ids: Sequence[uuid.UUID]) -> None:
        self.leaked_secret_ids = tuple(leaked_secret_ids)
        super().__init__(
            f"override text matches {len(self.leaked_secret_ids)} concealed secret(s); "
            "refusing to post it"
        )


class _RewrittenMessage(BaseModel):
    content_md: str


@dataclass(frozen=True)
class OverrideDraft:
    """What a human is about to say as an agent, before they confirm it. Nothing here is
    persisted: a draft the human abandons leaves no trace, which is the same
    draft-and-approve shape E2.2's secret drafting and F3.12's edit proposals use."""

    persona_id: uuid.UUID
    agent_principal_id: uuid.UUID
    mode: OverrideMode
    original_content_md: str
    proposed_content_md: str

    @property
    def rewrite_applied(self) -> bool:
        return self.mode == "voice"


async def _check_speak_as(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    permission_service: PermissionService,
) -> None:
    if not await permission_service.check(
        tenant_id, principal_id, _SPEAK_AS_ACTION, "workspace", workspace_id
    ):
        raise SpeakAsDeniedError(
            f"principal {principal_id} may not speak as an agent in workspace {workspace_id}"
        )


async def _persona_of(
    tenant_id: uuid.UUID, persona_id: uuid.UUID, workspace_id: uuid.UUID
) -> Persona:
    async with tenant_scope(tenant_id) as session:
        persona = await session.get(Persona, persona_id)
        if persona is None or persona.workspace_id != workspace_id:
            raise ValueError(f"no persona {persona_id} in workspace {workspace_id} for this tenant")
        session.expunge(persona)
        return persona


async def draft_override(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    persona_id: uuid.UUID,
    human_principal_id: uuid.UUID,
    content_md: str,
    *,
    mode: OverrideMode,
    permission_service: PermissionService,
    provider: ModelProvider | None = None,
    agent: Agent | None = None,
    api_key: str | None = None,
) -> OverrideDraft:
    """Verbatim mode returns the human's text unchanged and makes no model call.

    Voice mode calls the model once (``purpose='rewrite'``, so the egress policy applies
    exactly as it does to any other rewrite) and meters it whether or not the human goes
    on to confirm -- the tokens were spent either way, and a metering scheme that only
    charges for approved output would under-report real cost."""
    await _check_speak_as(tenant_id, workspace_id, human_principal_id, permission_service)
    persona = await _persona_of(tenant_id, persona_id, workspace_id)

    if mode == "verbatim":
        return OverrideDraft(
            persona_id=persona_id,
            agent_principal_id=persona.principal_id,
            mode="verbatim",
            original_content_md=content_md,
            proposed_content_md=content_md,
        )

    if provider is None or agent is None:
        raise ValueError("voice-conformance mode requires a provider and a connection agent")

    model_string = f"{agent.provider}/{agent.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Character:\n{persona.persona_md}\n\nMessage:\n{content_md}",
            },
        ],
        purpose=_REWRITE_PURPOSE,
        max_tokens=800,
        api_base=agent.api_base,
        params=dict(agent.params or {}),
        api_key=api_key,
    )

    start = time.monotonic()
    result = await provider.generate_structured(req, _RewrittenMessage)
    latency_ms = int((time.monotonic() - start) * 1000)

    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent.id,
                provider=agent.provider,
                model=agent.model,
                purpose=_REWRITE_PURPOSE,
                prompt_tokens=sum(
                    provider.count_tokens(str(m.get("content") or ""), model_string)
                    for m in req.messages
                ),
                completion_tokens=provider.count_tokens(result.content_md, model_string),
                latency_ms=latency_ms,
            )
        )

    return OverrideDraft(
        persona_id=persona_id,
        agent_principal_id=persona.principal_id,
        mode="voice",
        original_content_md=content_md,
        proposed_content_md=result.content_md,
    )


async def post_override(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    draft: OverrideDraft,
    human_principal_id: uuid.UUID,
    *,
    confirmed_content_md: str,
    permission_service: PermissionService,
    concealed_secrets: Sequence[ConcealedSecret] = (),
    embedding_provider: EmbeddingProvider | None = None,
) -> MessageRow:
    """Writes the override as the agent's turn. ``confirmed_content_md`` is what the human
    actually approved -- passed explicitly rather than read off the draft, because in voice
    mode the whole point is that a human saw the rewrite and said yes to *it*, possibly
    after editing it further.

    Same peek-then-claim ``next_event_seq`` shape as ``submit_human_turn``: the message,
    its session event, and the sequence claim are one transaction. The caller re-invokes
    ``advance_session`` afterwards exactly as it would for any other human turn -- the
    scheduler sees the agent's principal as author and advances identically, which is what
    makes this an override rather than an interruption."""
    await _check_speak_as(tenant_id, workspace_id, human_principal_id, permission_service)

    if concealed_secrets and embedding_provider is not None:
        leak = await check_for_leak(
            confirmed_content_md, concealed_secrets, embedding_provider=embedding_provider
        )
        if leak.leaked:
            await _write_override_leak_alert(tenant_id, session_id, leak.leaked_secret_ids)
            raise OverrideLeakError(leak.leaked_secret_ids)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        event_seq = row.next_event_seq
        row.next_event_seq = event_seq + 1

        message = MessageRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            author_principal_id=draft.agent_principal_id,
            role="assistant",
            content_md=confirmed_content_md,
            was_human_override=True,
            rewrite_applied=draft.rewrite_applied,
            original_content_md=draft.original_content_md if draft.rewrite_applied else None,
            overridden_by_principal_id=human_principal_id,
        )
        session.add(message)
        await session.flush()

        author_name = await resolve_author_name(session, tenant_id, draft.agent_principal_id)
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="human_override",
                payload={
                    "id": str(message.id),
                    "role": "assistant",
                    "content": confirmed_content_md,
                    "persona_id": str(draft.persona_id),
                    "rewrite_applied": draft.rewrite_applied,
                    "author": author_name,
                },
                actor_principal_id=human_principal_id,
            )
        )
        await session.flush()
        session.expunge(message)
        return message


async def _write_override_leak_alert(
    tenant_id: uuid.UUID, session_id: uuid.UUID, leaked_secret_ids: Sequence[uuid.UUID]
) -> None:
    """The same ``secret_leak_alert`` event kind E2.7 writes -- one alert stream for the
    overseer, whether the near-miss came from a model or from a person."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        event_seq = row.next_event_seq
        row.next_event_seq = event_seq + 1
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="secret_leak_alert",
                payload={
                    "secret_ids": [str(s) for s in leaked_secret_ids],
                    "source": "human_override",
                },
            )
        )


async def override_badge_visible_to_participants(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    """Whether participants see the override badge. Defaults to **True** -- the honest
    default. A workspace that wants the illusion preserved (a table where the director
    voicing an NPC would break immersion) can turn it off; the overseer's view never
    consults this setting at all."""
    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError(f"no workspace {workspace_id} in this tenant")
        value = workspace.settings.get(_BADGE_SETTING, True)
    return bool(value)


async def get_override_pair(
    tenant_id: uuid.UUID, message_id: uuid.UUID
) -> tuple[str | None, str] | None:
    """``(original, posted)`` for an override, for the overseer's review surface.
    ``None`` if the message isn't an override; ``original`` is ``None`` for a verbatim
    one, where the two would be the same string and storing it twice would only invite
    them to drift."""
    async with tenant_scope(tenant_id) as session:
        message = await session.get(MessageRow, message_id)
        if message is None or not message.was_human_override:
            return None
        return message.original_content_md, message.content_md


async def list_override_messages(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[MessageRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(MessageRow)
                .where(
                    MessageRow.session_id == session_id,
                    MessageRow.was_human_override.is_(True),
                )
                .order_by(MessageRow.event_seq)
            )
        ).scalars()
        return list(rows)
