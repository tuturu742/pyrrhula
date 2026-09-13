"""B1.8: the real persona_type-based candidate resolver -- the scheduler injection seam
core.process.scheduler.make_default_candidate_resolver has had since B1.3.
"""

from __future__ import annotations

import uuid

from core.agents.authoring import get_persona
from core.agents.scheduling import make_persona_candidate_resolver
from core.agents.seed import seed_dev_agent
from core.process.dsl.schema import ActorSpec, PhaseSpec, VisibilitySpec
from core.process.interpreter import InterpreterContext
from core.tenancy.seed import seed_dev_tenant

_VISIBILITY = VisibilitySpec(
    knowledge_classes=["rules"], scopes=["workspace_public"], entity_fields="all", secrets="none"
)


def _phase(actors: list[ActorSpec]) -> PhaseSpec:
    return PhaseSpec(label_key="phase.test", actors=actors, visibility=_VISIBILITY)


async def test_resolves_agent_role_directly(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"sched-agents-role-{uuid.uuid4().hex[:8]}"
    )
    facilitator_id = await seed_dev_agent(
        tenant_id, workspace_id, key="facilitator", persona_type="supervisor"
    )
    participant_id = await seed_dev_agent(
        tenant_id, workspace_id, key="participant", persona_type="participant"
    )

    resolver = make_persona_candidate_resolver(tenant_id, workspace_id)
    spec = ActorSpec(persona_type="supervisor", mode="generate")
    phase = _phase([spec])
    ctx = InterpreterContext(tenant_id, uuid.uuid4(), "test_phase", phase, {})

    candidates = await resolver(spec, ctx)

    facilitator = await get_persona(tenant_id, facilitator_id)
    participant = await get_persona(tenant_id, participant_id)
    assert facilitator is not None and participant is not None
    assert [c.principal_id for c in candidates] == [facilitator.principal_id]


async def test_resolves_any_of_agent_tokens(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"sched-agents-anyof-{uuid.uuid4().hex[:8]}"
    )
    facilitator_id = await seed_dev_agent(
        tenant_id, workspace_id, key="facilitator", persona_type="supervisor"
    )
    participant_id = await seed_dev_agent(
        tenant_id, workspace_id, key="participant", persona_type="participant"
    )

    facilitator = await get_persona(tenant_id, facilitator_id)
    participant = await get_persona(tenant_id, participant_id)
    assert facilitator is not None and participant is not None

    resolver = make_persona_candidate_resolver(tenant_id, workspace_id)
    spec = ActorSpec(any_of=["supervisor_agent", "participant_agent"], mode="generate")
    phase = _phase([spec])
    ctx = InterpreterContext(tenant_id, uuid.uuid4(), "test_phase", phase, {})

    candidates = await resolver(spec, ctx)

    assert {c.principal_id for c in candidates} == {
        facilitator.principal_id,
        participant.principal_id,
    }


async def test_any_of_human_token_is_ignored_by_the_agent_resolver(db_available: None) -> None:
    """The agent resolver is only ever consulted for the *agent* half of a mixed
    human+agent any_of list -- core.process.scheduler.make_default_candidate_resolver
    is what dispatches human tokens to its own real workspace_membership query instead."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"sched-agents-human-{uuid.uuid4().hex[:8]}"
    )
    await seed_dev_agent(tenant_id, workspace_id, key="facilitator", persona_type="supervisor")

    resolver = make_persona_candidate_resolver(tenant_id, workspace_id)
    spec = ActorSpec(any_of=["human_participant"], mode="free")
    phase = _phase([spec])
    ctx = InterpreterContext(tenant_id, uuid.uuid4(), "test_phase", phase, {})

    candidates = await resolver(spec, ctx)
    assert candidates == []


async def test_initiative_with_no_explicit_selector_returns_empty(db_available: None) -> None:
    """No Entity system exists in Phase 1 -- 'implicit eligibility: whoever has the
    field' has nothing to query against, an honest empty result, not a guess."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"sched-agents-initiative-{uuid.uuid4().hex[:8]}"
    )
    await seed_dev_agent(tenant_id, workspace_id, key="facilitator", persona_type="supervisor")

    resolver = make_persona_candidate_resolver(tenant_id, workspace_id)
    spec = ActorSpec(order="initiative", mode="generate", from_field='entity_field("initiative")')
    phase = _phase([spec])
    ctx = InterpreterContext(tenant_id, uuid.uuid4(), "test_phase", phase, {})

    candidates = await resolver(spec, ctx)
    assert candidates == []


async def test_any_of_preserves_the_declared_role_order(db_available: None) -> None:
    """`order: "declared"` means the order the author wrote, not alphabetical.

    The resolver put the role tokens in a set and then sorted them, so
    ["supervisor_agent", "participant_agent"] scheduled every participant ahead of the
    supervisor. A phase meant to be opened by its facilitator instead opened with
    whichever participant sorted first -- seen in the Hägnaryd sample, where a suspect
    spoke first and the investigator leading the interrogation took the sixth turn.
    """
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"sched-order-{uuid.uuid4().hex[:8]}"
    )
    facilitator_id = await seed_dev_agent(
        tenant_id, workspace_id, key="facilitator", persona_type="supervisor"
    )
    await seed_dev_agent(tenant_id, workspace_id, key="participant", persona_type="participant")
    facilitator = await get_persona(tenant_id, facilitator_id)
    assert facilitator is not None

    resolver = make_persona_candidate_resolver(tenant_id, workspace_id)
    spec = ActorSpec(any_of=["supervisor_agent", "participant_agent"], mode="generate")
    phase = _phase([spec])
    ctx = InterpreterContext(tenant_id, uuid.uuid4(), "test_phase", phase, {})

    candidates = await resolver(spec, ctx)

    assert candidates[0].principal_id == facilitator.principal_id, (
        "the first declared role has to come first"
    )


async def test_any_of_reversed_puts_participants_first(db_available: None) -> None:
    """The mirror image: declaring participants first must actually do that, or the
    fix is just a different hardcoded order."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"sched-order-rev-{uuid.uuid4().hex[:8]}"
    )
    await seed_dev_agent(tenant_id, workspace_id, key="facilitator", persona_type="supervisor")
    participant_id = await seed_dev_agent(
        tenant_id, workspace_id, key="participant", persona_type="participant"
    )
    participant = await get_persona(tenant_id, participant_id)
    assert participant is not None

    resolver = make_persona_candidate_resolver(tenant_id, workspace_id)
    spec = ActorSpec(any_of=["participant_agent", "supervisor_agent"], mode="generate")
    phase = _phase([spec])
    ctx = InterpreterContext(tenant_id, uuid.uuid4(), "test_phase", phase, {})

    candidates = await resolver(spec, ctx)

    assert candidates[0].principal_id == participant.principal_id
