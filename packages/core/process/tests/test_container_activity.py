"""`container_activity`: what ran under *this* persona's name.

The property worth testing is the scoping, and specifically that it is enforced rather
than requested. A tool that returned everyone's containers and relied on the model to only
read its own would be exactly the kind of "ask the model nicely" this project exists not to
do.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent, create_persona
from core.agents.tools import ToolContext
from core.exec_envs import track_run_end, track_run_start
from core.process.session_container_tools import make_container_activity_handler
from core.process.skeleton import create_session
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _workspace(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _persona_in_session(tenant_id: uuid.UUID, key: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A real persona and a real session. ``exec_environment.session_id`` carries a real
    foreign key, so a plausible-looking uuid will not do."""
    workspace_id = await _workspace(tenant_id)
    agent = await create_agent(
        tenant_id,
        name=f"conn-{key}",
        provider="echo",
        model="echo-1",
        encryptor=IdentityEncryptor(),
    )
    persona = await create_persona(
        tenant_id,
        workspace_id,
        key=key,
        name=key.title(),
        agent_id=agent.id,
        persona_type="participant",
        persona_md="A developer.",
    )
    row = await create_session(tenant_id, workspace_id, persona.id)
    return persona.id, row.id


async def _call(tenant_id: uuid.UUID, persona_id: uuid.UUID, session_id: uuid.UUID | None) -> dict:
    handler = make_container_activity_handler()
    result = await handler(
        {}, ToolContext(tenant_id=tenant_id, persona_id=persona_id, session_id=session_id)
    )
    return json.loads(result.content)


async def _ran(
    tenant_id: uuid.UUID,
    name: str,
    persona_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    exit_code: int | None = 0,
) -> None:
    await track_run_start(
        tenant_id,
        name,
        engine_key="local",
        image="docker.io/library/node:20-bookworm",
        session_id=session_id,
        persona_id=persona_id,
        label="Dev",
    )
    if exit_code is not None:
        await track_run_end(tenant_id, name, exit_code=exit_code)


async def test_it_reports_this_personas_own_environment(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"ca-own-{uuid.uuid4().hex[:8]}")
    persona_id, session_id = await _persona_in_session(tenant_id, "dev")
    await _ran(tenant_id, "pyr-env-abc-proj", persona_id, session_id)

    payload = await _call(tenant_id, persona_id, session_id)
    assert len(payload["environments"]) == 1
    env = payload["environments"][0]
    assert env["environment"] == "pyr-env-abc-proj"
    assert env["last_exit_code"] == 0
    assert env["engine"] == "local"


async def test_another_personas_container_is_not_visible(db_available: None) -> None:
    """The point. Two developers in one session each have their own environment, and
    neither should be able to account for the other's work as its own."""
    tenant_id, _, _ = await seed_dev_tenant(slug=f"ca-mine-{uuid.uuid4().hex[:8]}")
    theirs, session_id = await _persona_in_session(tenant_id, "other")
    mine, _ = await _persona_in_session(tenant_id, "mine")
    await _ran(tenant_id, "pyr-env-theirs", theirs, session_id, exit_code=None)

    payload = await _call(tenant_id, mine, session_id)
    assert payload["environments"] == []
    assert "no execution environment has run under your name" in payload["note"]


async def test_nothing_is_said_plainly_rather_than_implied(db_available: None) -> None:
    """ "Nothing" has two causes -- never started, or still running -- and a model that
    cannot tell them apart will pick one and state it with confidence."""
    tenant_id, _, _ = await seed_dev_tenant(slug=f"ca-none-{uuid.uuid4().hex[:8]}")
    payload = await _call(tenant_id, uuid.uuid4(), uuid.uuid4())
    assert payload["environments"] == []
    assert "may still be in progress" in payload["note"]


async def test_another_tenants_environment_is_not_visible(db_available: None) -> None:
    """Belt and braces over RLS: the same persona id in two tenants must not bleed."""
    suffix = uuid.uuid4().hex[:8]
    mine, _, _ = await seed_dev_tenant(slug=f"ca-a-{suffix}")
    other, _, _ = await seed_dev_tenant(slug=f"ca-b-{suffix}")
    persona_id, session_id = await _persona_in_session(other, "dev")
    await _ran(other, "pyr-env-other-tenant", persona_id, session_id, exit_code=None)

    assert (await _call(mine, persona_id, session_id))["environments"] == []


async def test_it_accounts_for_delegated_spend(db_available: None) -> None:
    """What the persona's connection actually cost on delegated work -- read from
    usage_record, which the proxy writes, rather than from anything the harness said about
    itself."""
    from core.audit.models import UsageRecordRow

    tenant_id, _, _ = await seed_dev_tenant(slug=f"ca-spend-{uuid.uuid4().hex[:8]}")
    persona_id, session_id = await _persona_in_session(tenant_id, "dev")
    await _ran(tenant_id, "pyr-env-spend", persona_id, session_id)
    async with tenant_scope(tenant_id) as session:
        for prompt, completion, purpose in ((100, 10, "delegation"), (7, 3, "generation")):
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    persona_id=persona_id,
                    provider="p",
                    model="m",
                    purpose=purpose,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )

    payload = await _call(tenant_id, persona_id, session_id)
    # Only delegated spend: this tool answers for containers, and a persona's ordinary
    # turns are not container work.
    assert payload["delegated_spend"] == {"prompt_tokens": 100, "completion_tokens": 10}
