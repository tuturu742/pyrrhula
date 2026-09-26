"""the acceptance criteria for the post-generation leak check: a leaking reply
regenerates exactly once then falls back with an overseer alert, a clean reply incurs no
regeneration, and only secrets the caller marks concealed are ever flagged -- revealed/
public secrets are never in scope, structurally, since the caller controls the
`concealed_secrets` list this module checks against.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.agents.seed import seed_dev_agent
from core.ports.embedding import EmbedRequest
from core.process.skeleton import create_session
from core.secrets.leak_check import ConcealedSecret, run_post_generation_check
from core.sessions.models import SessionEventRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_SECRET_CONTENT = "the missing heir is actually the innkeeper in disguise all along"


class _FixedVectorEmbeddingProvider:
    """Maps specific texts to specific vectors under full test control -- a hash-based
    stub (`StubEmbeddingProvider`, A1.3) can't reliably produce "this text is
    semantically close to that one" on demand, which is exactly what these tests need."""

    def __init__(self, vectors: dict[str, list[float]], default: list[float]) -> None:
        self._vectors = vectors
        self._default = default

    @property
    def model_name(self) -> str:
        return "local/fixed-vector-stub"

    @property
    def dimension(self) -> int:
        return len(self._default)

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        return [self._vectors.get(text, self._default) for text in req.texts]


class _ScriptedRegenerate:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.call_count = 0

    async def __call__(self) -> str:
        self.call_count += 1
        return self._replies.pop(0)


async def test_leak_flag_regenerates_once_then_falls_back_with_overseer_alert(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"leakcheck-fallback-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    secret_id = uuid.uuid4()
    leaking_reply = "the missing heir is actually the innkeeper in disguise, surprise!"
    still_leaking_reply = "turns out the missing heir is actually the innkeeper, yes"

    # Both replies embed identically close to the secret's content; the fuzzy check
    # alone would already catch this (shared word run), so the embedding signal here is
    # redundant on purpose -- either detector firing is sufficient per the module's own
    # "errs toward over-flagging" design.
    provider = _FixedVectorEmbeddingProvider(
        vectors={
            _SECRET_CONTENT: [1.0, 0.0],
            leaking_reply: [1.0, 0.0],
            still_leaking_reply: [1.0, 0.0],
        },
        default=[0.0, 1.0],
    )
    regenerate = _ScriptedRegenerate([still_leaking_reply])

    outcome = await run_post_generation_check(
        tenant_id,
        sess.id,
        0,
        leaking_reply,
        [ConcealedSecret(secret_id=secret_id, content=_SECRET_CONTENT)],
        embedding_provider=provider,
        regenerate=regenerate,
    )

    assert outcome.outcome == "fallback"
    assert outcome.fallback_used is True
    assert outcome.regenerated is True
    assert regenerate.call_count == 1, "must never attempt a third generation"
    assert outcome.final_content != leaking_reply
    assert outcome.final_content != still_leaking_reply
    assert secret_id in outcome.leaked_secret_ids


async def test_clean_reply_incurs_no_regeneration(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"leakcheck-clean-{uuid.uuid4().hex[:8]}"
    )
    secret_id = uuid.uuid4()
    clean_reply = "the weather in the harbor district has been unusually calm lately"

    provider = _FixedVectorEmbeddingProvider(
        vectors={_SECRET_CONTENT: [1.0, 0.0], clean_reply: [0.0, 1.0]},
        default=[0.0, 1.0],
    )
    regenerate = _ScriptedRegenerate([])

    outcome = await run_post_generation_check(
        tenant_id,
        uuid.uuid4(),
        0,
        clean_reply,
        [ConcealedSecret(secret_id=secret_id, content=_SECRET_CONTENT)],
        embedding_provider=provider,
        regenerate=regenerate,
    )

    assert outcome.outcome == "clean"
    assert outcome.final_content == clean_reply
    assert regenerate.call_count == 0, "a clean reply must trigger no extra model call"
    assert outcome.leaked_secret_ids == ()


async def test_revealed_secrets_are_not_flagged_by_the_leak_check(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"leakcheck-revealed-{uuid.uuid4().hex[:8]}"
    )
    # The reply states the fact verbatim -- if this secret were still in the concealed
    # set, it would trigger immediately. It is deliberately *not* passed to
    # concealed_secrets at all (the caller's job, per E2.6, is to only ever include
    # secrets whose disposition this turn is conceal/hint -- a revealed or public
    # secret is never a candidate here in the first place, structurally, not by a
    # runtime "is this revealed?" check this module would otherwise need).
    reply_stating_the_fact = _SECRET_CONTENT
    provider = _FixedVectorEmbeddingProvider(vectors={}, default=[1.0, 0.0])
    regenerate = _ScriptedRegenerate([])

    outcome = await run_post_generation_check(
        tenant_id,
        uuid.uuid4(),
        0,
        reply_stating_the_fact,
        [],  # no concealed secrets this turn -- e.g. the only secret in play was revealed
        embedding_provider=provider,
        regenerate=regenerate,
    )

    assert outcome.outcome == "clean"
    assert regenerate.call_count == 0
    assert outcome.leaked_secret_ids == ()


async def test_overseer_alert_is_recorded_as_a_session_event_on_fallback(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"leakcheck-alert-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    secret_id = uuid.uuid4()
    leaking_reply = "the missing heir is actually the innkeeper, in plain terms"
    provider = _FixedVectorEmbeddingProvider(
        vectors={_SECRET_CONTENT: [1.0, 0.0], leaking_reply: [1.0, 0.0]},
        default=[1.0, 0.0],
    )
    regenerate = _ScriptedRegenerate([leaking_reply])
    session_id = sess.id

    await run_post_generation_check(
        tenant_id,
        session_id,
        7,
        leaking_reply,
        [ConcealedSecret(secret_id=secret_id, content=_SECRET_CONTENT)],
        embedding_provider=provider,
        regenerate=regenerate,
    )

    async with tenant_scope(tenant_id) as session:
        event = await session.scalar(
            select(SessionEventRow).where(
                SessionEventRow.session_id == session_id,
                SessionEventRow.kind == "secret_leak_alert",
            )
        )
    assert event is not None
    secret_ids = event.payload["secret_ids"]
    assert isinstance(secret_ids, list)
    assert str(secret_id) in secret_ids
