"""Acceptance criteria: a human replies in place of an agent, the override
is recorded and surfaced, a voice rewrite posts only after confirmation and preserves the
original, and the leak check still runs on what a human typed.

Under ``tests/isolation/`` for the ``two_tenants`` fixture; no new RLS table here (the
override adds columns to `message`, which already has its own filter-omission coverage).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.agents.models import Persona
from core.agents.override import (
    OverrideLeakError,
    SpeakAsDeniedError,
    draft_override,
    get_override_pair,
    list_override_messages,
    override_badge_visible_to_participants,
    post_override,
)
from core.agents.seed import seed_dev_agent
from core.audit.models import UsageRecordRow
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.skeleton import create_session
from core.secrets.leak_check import ConcealedSecret
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()


@dataclass
class _ScriptedRewriter:
    rewritten: str

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        return schema.model_validate(dict.fromkeys(schema.model_fields, self.rewritten))

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


@dataclass
class _KeywordEmbeddings:
    """A deliberately crude embedder: two texts sharing the marker word land on the same
    vector. Enough for the leak check's embedding arm to fire on cue without a real model,
    and honest about being a fixture rather than a semantic claim."""

    marker: str
    model_name: str = field(default="fixture-embed")
    dimension: int = field(default=2)

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        return [[1.0, 0.0] if self.marker in t.lower() else [0.0, 1.0] for t in req.texts]


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=role)
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role=role,
            )
        )
        return principal.id


async def _setup(tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """``(workspace_id, session_id, persona_id, agent_principal_id)``."""
    workspace_id = await _workspace_of(tenant_id)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        assert agent is not None
        agent_principal_id = agent.principal_id
    return workspace_id, sess.id, persona_id, agent_principal_id


async def test_generate_as_is_permission_gated_and_advances_the_agents_turn(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id, session_id, persona_id, agent_principal_id = await _setup(tenant_id)
    participant_id = await _member(tenant_id, workspace_id, "participant")
    facilitator_id = await _member(tenant_id, workspace_id, "facilitator")

    # A participant may not put words in an agent's mouth.
    with pytest.raises(SpeakAsDeniedError):
        await draft_override(
            tenant_id,
            workspace_id,
            persona_id,
            participant_id,
            "I am the innkeeper.",
            mode="verbatim",
            permission_service=_PERMISSIONS,
        )

    async with tenant_scope(tenant_id) as session:
        before = await session.get(SessionRow, session_id)
        assert before is not None
        seq_before = before.next_event_seq

    draft = await draft_override(
        tenant_id,
        workspace_id,
        persona_id,
        facilitator_id,
        "The innkeeper shrugs and pours another.",
        mode="verbatim",
        permission_service=_PERMISSIONS,
    )
    message = await post_override(
        tenant_id,
        workspace_id,
        session_id,
        draft,
        facilitator_id,
        confirmed_content_md=draft.proposed_content_md,
        permission_service=_PERMISSIONS,
    )

    # The scheduler and the transcript see the *agent's* turn: same author principal, same
    # event_seq claim, same role as a generated reply would have had.
    assert message.author_principal_id == agent_principal_id
    assert message.overridden_by_principal_id == facilitator_id
    assert message.role == "assistant"
    assert message.event_seq == seq_before

    async with tenant_scope(tenant_id) as session:
        after = await session.get(SessionRow, session_id)
        assert after is not None
        assert after.next_event_seq == seq_before + 1
        event = await session.scalar(
            select(SessionEventRow).where(
                SessionEventRow.session_id == session_id,
                SessionEventRow.event_seq == message.event_seq,
            )
        )
        assert event is not None
        assert event.kind == "human_override"
        assert event.actor_principal_id == facilitator_id


async def test_rewrite_requires_confirmation_and_preserves_the_original(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id, session_id, persona_id, _agent_principal_id = await _setup(tenant_id)
    facilitator_id = await _member(tenant_id, workspace_id, "facilitator")
    profile = await create_agent(
        tenant_id,
        f"override-{uuid.uuid4().hex[:6]}",
        "echo",
        "echo-1",
        encryptor=IdentityEncryptor(),
    )

    typed = "tell them the bridge is out"
    polished = "The bridge, I'm afraid, has gone the way of all timber. You'll want the ford."
    draft = await draft_override(
        tenant_id,
        workspace_id,
        persona_id,
        facilitator_id,
        typed,
        mode="voice",
        permission_service=_PERMISSIONS,
        provider=_ScriptedRewriter(polished),
        agent=profile,
    )

    assert draft.rewrite_applied is True
    assert draft.original_content_md == typed
    assert draft.proposed_content_md == polished

    # Drafting posts nothing: the human has not confirmed yet.
    async with tenant_scope(tenant_id) as session:
        assert (
            await session.scalar(select(MessageRow).where(MessageRow.session_id == session_id))
        ) is None
        usage = (
            await session.execute(
                select(UsageRecordRow.purpose).where(UsageRecordRow.tenant_id == tenant_id)
            )
        ).scalars()
        assert all(p == "rewrite" for p in usage)

    message = await post_override(
        tenant_id,
        workspace_id,
        session_id,
        draft,
        facilitator_id,
        confirmed_content_md=draft.proposed_content_md,
        permission_service=_PERMISSIONS,
    )

    assert message.rewrite_applied is True
    assert message.content_md == polished
    assert message.original_content_md == typed
    assert await get_override_pair(tenant_id, message.id) == (typed, polished)


async def test_override_flag_recorded_and_surfaced(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id, session_id, persona_id, _agent_principal_id = await _setup(tenant_id)
    facilitator_id = await _member(tenant_id, workspace_id, "facilitator")
    profile = await create_agent(
        tenant_id, f"flag-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    posted: list[MessageRow] = []
    for mode, provider in (("verbatim", None), ("voice", _ScriptedRewriter("in voice"))):
        draft = await draft_override(
            tenant_id,
            workspace_id,
            persona_id,
            facilitator_id,
            f"typed in {mode} mode",
            mode=mode,  # type: ignore[arg-type]
            permission_service=_PERMISSIONS,
            provider=provider,
            agent=profile if mode == "voice" else None,
        )
        posted.append(
            await post_override(
                tenant_id,
                workspace_id,
                session_id,
                draft,
                facilitator_id,
                confirmed_content_md=draft.proposed_content_md,
                permission_service=_PERMISSIONS,
            )
        )

    # Never false for an override -- through both modes.
    assert [m.was_human_override for m in posted] == [True, True]
    assert [m.rewrite_applied for m in posted] == [False, True]
    # Verbatim keeps no separate original: it would be the same string, stored twice, free
    # to drift.
    assert posted[0].original_content_md is None
    assert await get_override_pair(tenant_id, posted[0].id) == (None, posted[0].content_md)

    listed = await list_override_messages(tenant_id, session_id)
    assert [m.id for m in listed] == [m.id for m in posted]

    # The honest default: participants see the badge unless a workspace turns it off.
    assert await override_badge_visible_to_participants(tenant_id, workspace_id) is True
    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.settings = {
            **workspace.settings,
            "show_override_badge_to_participants": False,
        }
    assert await override_badge_visible_to_participants(tenant_id, workspace_id) is False


async def test_human_override_still_passes_the_leak_check(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id, session_id, persona_id, _agent_principal_id = await _setup(tenant_id)
    facilitator_id = await _member(tenant_id, workspace_id, "facilitator")

    secret_id = uuid.uuid4()
    concealed: Sequence[ConcealedSecret] = (
        ConcealedSecret(
            secret_id=secret_id,
            content=(
                "the innkeeper poisoned the baron at the harvest feast using nightshade "
                "from the abbey garden"
            ),
        ),
    )
    embeddings = _KeywordEmbeddings(marker="nightshade")

    leaking = await draft_override(
        tenant_id,
        workspace_id,
        persona_id,
        facilitator_id,
        "Fine, the truth: the innkeeper poisoned the baron at the harvest feast using "
        "nightshade from the abbey garden.",
        mode="verbatim",
        permission_service=_PERMISSIONS,
    )
    with pytest.raises(OverrideLeakError) as exc:
        await post_override(
            tenant_id,
            workspace_id,
            session_id,
            leaking,
            facilitator_id,
            confirmed_content_md=leaking.proposed_content_md,
            permission_service=_PERMISSIONS,
            concealed_secrets=concealed,
            embedding_provider=embeddings,
        )
    assert exc.value.leaked_secret_ids == (secret_id,)

    # Refused, not substituted: no message exists, and the overseer has an alert.
    async with tenant_scope(tenant_id) as session:
        assert (
            await session.scalar(select(MessageRow).where(MessageRow.session_id == session_id))
        ) is None
        alert = await session.scalar(
            select(SessionEventRow).where(
                SessionEventRow.session_id == session_id,
                SessionEventRow.kind == "secret_leak_alert",
            )
        )
        assert alert is not None
        assert alert.payload["secret_ids"] == [str(secret_id)]
        assert alert.payload["source"] == "human_override"

    # A clean override through the same path posts normally -- the check is a tripwire,
    # not a gate that refuses everything once armed.
    clean = await draft_override(
        tenant_id,
        workspace_id,
        persona_id,
        facilitator_id,
        "The innkeeper wipes down the bar and says nothing.",
        mode="verbatim",
        permission_service=_PERMISSIONS,
    )
    message = await post_override(
        tenant_id,
        workspace_id,
        session_id,
        clean,
        facilitator_id,
        confirmed_content_md=clean.proposed_content_md,
        permission_service=_PERMISSIONS,
        concealed_secrets=concealed,
        embedding_provider=embeddings,
    )
    assert message.was_human_override is True
