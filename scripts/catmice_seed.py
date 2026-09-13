"""Seed the cat-vs-mice build for agents (run inside the api pod).

Reads a base64 JSON payload on argv: the scaffold file map and the work items, so the
GDScript stays readable in ``scripts/gamedev_scaffold.py`` instead of being inlined here.

Idempotent. Resets ``scripts/rules.gd`` and ``scripts/game.gd`` back to their stubs on
every run -- a rerun must hand the agents the same starting point, or "CI went green"
would just mean the previous run's code is still there.

Prints ``key=value`` lines for the host driver.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid

from sqlalchemy import select, text

from adapters.mcp.git_store import GitStore, default_git_root
from core.agents.authoring import create_persona
from core.agents.models import Agent, Persona
from core.entities.repo import get_latest_schema_version
from core.entities.storage import EntityRow, create_entity
from core.repos.service import create_repo, store_key
from core.tenancy.models import Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope, unscoped_session

SLUG = "gamedev"
REPO_KEY = "vgame"

TEST_CMD = (
    "godot --headless --path . --import >/dev/null 2>&1 || true; "
    "godot --headless --path . --script tests/run_tests.gd"
)
BUILD_CMD = (
    "godot --headless --path . --import >/dev/null 2>&1 || true; "
    "mkdir -p build/web && godot --headless --path . --export-release Web "
    "build/web/index.html && tar czf /tmp/game-web.tar.gz -C build/web . "
    "&& mv /tmp/game-web.tar.gz game-web.tar.gz"
)

LEAD_MD = (
    "You lead a two-person game team building a small browser game. You plan the work, "
    "hand each piece to the developer, and check the result against what the tests say. "
    "Be concise and concrete; name files and functions rather than describing them."
)
DEV_MD = (
    "You are the developer on a small game team. You write Godot 4.3 GDScript. You keep "
    "gameplay rules in pure static functions so they can be tested headlessly, and you "
    "annotate types explicitly because ':=' cannot infer from max()/abs() or from "
    "untyped Array elements."
)


async def _tenant() -> tuple[uuid.UUID, uuid.UUID]:
    async with unscoped_session() as session:
        tid = await session.scalar(text("SELECT id FROM tenant WHERE slug=:s"), {"s": SLUG})
    if tid is None:
        raise SystemExit(f"tenant {SLUG!r} does not exist -- run gamedev_seed.py first")
    async with tenant_scope(tid) as session:
        wid = await session.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))
    return tid, wid


async def _connection(tid: uuid.UUID, name: str):
    """Look up a connection the driver already created through /model-profiles.

    Deliberately a lookup, not a create: /model-profiles writes to this same `agent`
    table, so creating one here too would leave the persona bound to a second row with
    no sealed credential -- which fails only later, as a 401 mid-run."""
    async with tenant_scope(tid) as session:
        agent_id = await session.scalar(
            select(Agent.id).where(Agent.name == name, Agent.archived_at.is_(None))
        )
    if agent_id is None:
        raise SystemExit(f"connection {name!r} not found -- the driver must create it first")
    return agent_id


async def _persona(
    tid: uuid.UUID,
    wid: uuid.UUID,
    key: str,
    name: str,
    agent_id,
    persona_type: str,
    persona_md: str,
) -> uuid.UUID:
    async with tenant_scope(tid) as session:
        persona = await session.scalar(
            select(Persona).where(
                Persona.workspace_id == wid, Persona.key == key, Persona.archived_at.is_(None)
            )
        )
    if persona is None:
        persona = await create_persona(
            tid,
            wid,
            key,
            name,
            agent_id,
            persona_type=persona_type,
            persona_md=persona_md,
        )
    async with tenant_scope(tid) as session:
        row = await session.get(Persona, persona.id)
        row.agent_id = agent_id
        row.persona_md = persona_md
        member = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == wid,
                WorkspaceMembership.principal_id == row.principal_id,
            )
        )
        if member is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tid,
                    workspace_id=wid,
                    principal_id=row.principal_id,
                    role="facilitator" if persona_type == "supervisor" else "participant",
                )
            )
    return persona.id


async def _repo(tid: uuid.UUID, scaffold: dict[str, str]) -> uuid.UUID:
    from core.repos.models import RepoRow

    async with tenant_scope(tid) as session:
        repo_id = await session.scalar(
            select(RepoRow.id).where(RepoRow.key == REPO_KEY, RepoRow.archived_at.is_(None))
        )
        if repo_id is not None:
            row = await session.get(RepoRow, repo_id)
            row.name = "Cat vs Mice"
            row.test_cmd, row.build_cmd = TEST_CMD, BUILD_CMD
            row.artifact_name = "game-web.tar.gz"
            row.runtime = "custom"
            row.runtime_image = "docker.io/barichello/godot-ci:4.3"
    if repo_id is None:
        created = await create_repo(
            tid,
            REPO_KEY,
            "Cat vs Mice",
            runtime="custom",
            runtime_image="docker.io/barichello/godot-ci:4.3",
            test_cmd=TEST_CMD,
            build_cmd=BUILD_CMD,
            artifact_name="game-web.tar.gz",
        )
        repo_id = created.id

    store = GitStore(default_git_root())
    key = store_key(tid, REPO_KEY)
    await store.ensure_repo(key)
    # Always reset to the stubs: a rerun must start the agents from the same place.
    await store.import_tree(key, scaffold)
    return repo_id


async def _work_items(tid: uuid.UUID, wid: uuid.UUID, items: list[dict]) -> list[uuid.UUID]:
    schema = await get_latest_schema_version(tid, wid, "work_item") or (
        await get_latest_schema_version(tid, None, "work_item")
    )
    if schema is None:
        raise SystemExit("work_item schema missing (swdev pack not loaded)")
    out: list[uuid.UUID] = []
    for item in items:
        async with tenant_scope(tid) as session:
            existing = await session.scalar(
                select(EntityRow.id).where(
                    EntityRow.workspace_id == wid, EntityRow.key == item["key"]
                )
            )
        if existing is None:
            row = await create_entity(
                tid,
                wid,
                schema.id,
                schema.to_definition(),
                item["key"],
                item["name"],
                "workspace_public",
                {
                    "title": item["name"],
                    "description": item["description"],
                    "original_estimate": 2,
                    "remaining_estimate": 2,
                    "story_points": 2,
                    "labels": [],
                },
            )
            existing = row.id
        async with tenant_scope(tid) as session:
            entity = await session.get(EntityRow, existing)
            entity.data = {**(entity.data or {}), "description": item["description"]}
            entity.fsm_states = {"lifecycle": "in_progress"}
        out.append(existing)
    return out


async def main() -> None:
    payload = json.loads(base64.b64decode(sys.argv[1]).decode())
    tid, wid = await _tenant()

    lead_agent = await _connection(tid, payload["lead_connection"])
    dev_agent = await _connection(tid, payload["dev_connection"])
    lead = await _persona(tid, wid, "cm-lead", "Mira (lead)", lead_agent, "supervisor", LEAD_MD)
    dev = await _persona(tid, wid, "cm-dev", "Tobias (dev)", dev_agent, "participant", DEV_MD)

    repo_id = await _repo(tid, payload["scaffold"])
    items = await _work_items(tid, wid, payload["work_items"])

    async with tenant_scope(tid) as session:
        definition_id = await session.scalar(
            text("SELECT id FROM process_definition WHERE tenant_id=:t LIMIT 1"), {"t": tid}
        )

    print(f"tenant={tid}")
    print(f"workspace={wid}")
    print(f"lead={lead}")
    print(f"dev={dev}")
    print(f"repo={repo_id}")
    print(f"store_key={store_key(tid, REPO_KEY)}")
    print(f"definition={definition_id}")
    for item, entity_id in zip(payload["work_items"], items, strict=True):
        print(f"item_{item['key']}={entity_id}")


asyncio.run(main())
