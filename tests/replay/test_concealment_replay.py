"""The INV-10 replay claim: a turn with concealments reproduces the exact same
rendered context (and therefore the exact same ``content_hash``) on a second call with
the same recorded inputs -- the same determinism property its own replay test proves
for retrieval, extended to cover the exclusion step.
"""

from __future__ import annotations

import uuid

from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import assemble
from core.knowledge.retrieval.tests.conftest import unit_vector
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.secrets.exclusion import ResolvedSecretDecision
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def test_replay_of_a_concealment_turn_is_byte_identical(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"concealment-replay-{uuid.uuid4().hex[:8]}"
    )
    async with tenant_scope(tenant_id) as session:
        viewer = Principal(tenant_id=tenant_id, kind="human", display_name="viewer")
        session.add(viewer)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=viewer.id,
                role="participant",
            )
        )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    phase = PhaseSpec(
        label_key="test_phase",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=["workspace_public"], entity_fields="all", secrets="none"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=100),
    )
    resolved = [
        ResolvedSecretDecision(
            secret_id=uuid.uuid4(),
            action="conceal",
            decision_id=None,
            content=None,
            hint_text=None,
            behavioral_directive="grows quiet whenever the topic comes up",
        ),
        ResolvedSecretDecision(
            secret_id=uuid.uuid4(),
            action="hint",
            decision_id=None,
            content=None,
            hint_text="something about an old debt",
            behavioral_directive="grows uneasy",
        ),
    ]

    first = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        query_text="ask about the debt",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        resolved_secret_decisions=resolved,
    )
    second = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        query_text="ask about the debt",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        resolved_secret_decisions=resolved,
    )

    assert first.rendered_context == second.rendered_context
    assert first.content_hash == second.content_hash
    assert first.redactions == second.redactions
