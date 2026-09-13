"""#4: a session's explicit roster. The scheduler resolves a phase's actors from the roster
(by persona_type), so a workspace persona left off the roster never gets a turn -- and a
session with no roster still falls back to the whole workspace (skeleton/legacy)."""

from __future__ import annotations

import uuid

from core.agents.scheduling import _personas_with_type
from core.agents.seed import seed_dev_agent
from core.process.skeleton import create_session
from core.sessions.lifecycle import (
    get_session,
    list_session_roster,
    set_session_agenda,
    set_session_roster,
)
from core.tenancy.seed import seed_dev_tenant


async def test_session_agenda_is_stored_and_editable(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"agenda-{uuid.uuid4().hex[:8]}")
    supervisor = await seed_dev_agent(tenant_id, workspace_id, key="sup", persona_type="supervisor")
    sess = await create_session(tenant_id, workspace_id, supervisor)

    await set_session_agenda(tenant_id, sess.id, "1. Kickoff\n2. Decide the release date")
    row = await get_session(tenant_id, sess.id)
    assert row is not None and "release date" in (row.agenda_md or "")

    await set_session_agenda(tenant_id, sess.id, None)
    row = await get_session(tenant_id, sess.id)
    assert row is not None and row.agenda_md is None


async def test_scheduler_draws_only_from_the_roster(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"roster-{uuid.uuid4().hex[:8]}")
    supervisor = await seed_dev_agent(tenant_id, workspace_id, key="sup", persona_type="supervisor")
    p1 = await seed_dev_agent(tenant_id, workspace_id, key="p1", persona_type="participant")
    p2 = await seed_dev_agent(tenant_id, workspace_id, key="p2", persona_type="participant")
    off_roster = await seed_dev_agent(tenant_id, workspace_id, key="p3", persona_type="participant")

    sess = await create_session(tenant_id, workspace_id, supervisor)
    await set_session_roster(tenant_id, sess.id, supervisor, [p1, p2])

    roster = await list_session_roster(tenant_id, sess.id)
    assert len(roster) == 3  # supervisor + 2 participants
    assert sum(1 for r in roster if r.is_supervisor) == 1

    # scheduler participants come from the roster only
    scoped = await _personas_with_type(tenant_id, workspace_id, "participant", session_id=sess.id)
    scoped_ids = {c.principal_id for c in scoped}

    async def _principal(pid: uuid.UUID) -> uuid.UUID:
        from core.agents.authoring import get_persona

        persona = await get_persona(tenant_id, pid)
        assert persona is not None
        return persona.principal_id

    assert scoped_ids == {await _principal(p1), await _principal(p2)}
    assert await _principal(off_roster) not in scoped_ids

    # a session with no roster falls back to every workspace participant
    bare = await create_session(tenant_id, workspace_id, supervisor)
    fallback = await _personas_with_type(tenant_id, workspace_id, "participant", session_id=bare.id)
    assert len(fallback) == 3  # p1, p2, off_roster
