"""Idempotent seeding for the deployment-verification scenarios (docs/deploy-verification.md).

Run inside a container that has the app deps + DB access, e.g.:

    podman cp scripts/seed_verification.py pyrrhula_api_1:/tmp/seed_verification.py
    podman exec pyrrhula_api_1 python /tmp/seed_verification.py exec

Seeds, per demo tenant (dev is already set up by hand): Ollama model connections, a persona
roster (1 supervisor + N participants), an overseer workspace_membership for the tenant owner
(session-acting is gated on that -- the owner does NOT get one automatically), and a launchable
copy of the domain-neutral round-table flow. rpg/swe additionally load their pack (and, later,
knowledge / workflow provisioning -- wired as those builds land). Re-runnable: every step checks
for an existing row first.

This is operational glue, not app code -- it lives in scripts/, imports core, and is never
imported by the app.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from sqlalchemy import select, text

from core.agents.authoring import create_agent, create_persona
from core.agents.models import Agent, Persona
from core.process.authoring import create_definition
from core.process.dsl.fixtures import AGENT_ROUND_TABLE_FLOW
from core.sessions.models import SessionRow  # noqa: F401 -- ensure mappers configure
from core.tenancy.models import Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope, unscoped_session

OLLAMA_BASE = "http://ollama:11434"

# Deterministic password the harness logs in with, set on the tenant owner's local identity.
# These are throwaway verification tenants owned by the runbook; resetting the demo owner's
# password to a known value is intentional (documented in docs/deploy-verification.md).
VERIFY_PASSWORD = "verify-pass-123"

# Small, fast model for participant discussion; a better instruction-follower for the
# supervisor, who writes the framing/regroup/synthesis deliverable (hermes3 rambles; qwen2.5
# holds a requested structure much better while staying fast). The big model is only worth its
# latency for the swe coding build (wired there later).
PARTICIPANT_MODEL = "hermes3:8b"
SUPERVISOR_MODEL = "qwen2.5:7b"
STRONG_MODEL = "qwen3.8:27b"

# One roster shape per scenario tenant: (supervisor, [participants]). Domain-neutral personas;
# the agenda (set at launch) carries the scenario specifics.
ROSTERS: dict[str, tuple[tuple[str, str], list[tuple[str, str]]]] = {
    "exec": (
        ("Chair", "You chair an executive meeting. Keep it focused; drive to a decision."),
        [
            ("Marketing", "You lead marketing. Push for a concrete campaign structure."),
            ("Finance", "You lead finance. Weigh cost, ROI, and budget realism."),
            ("Product", "You lead product. Tie the campaign to product strengths."),
        ],
    ),
    "rpg": (
        ("GM", "You are the game master. Narrate scenes, call for checks, run encounters."),
        [
            ("Aria", "You play Aria, a nimble rogue. Create your character, then adventure."),
            ("Bruk", "You play Bruk, a stout warrior. Create your character, then adventure."),
        ],
    ),
    "swe": (
        ("Lead", "Tech lead / facilitator. Frame the work, approve the plan, review the PRs."),
        [
            ("Dev1", "Senior engineer. Propose the implementation plan and take the hardest part."),
            ("Dev2", "Engineer. Take a component and implement it."),
            ("Dev3", "Engineer. Take a component and implement it."),
        ],
    ),
}

PACK_DIRS = {"rpg": "packs/rpg", "swe": "packs/swdev"}

# swe: 1 strong model + 2 small (the first participant is the strong one).
STRONG_PARTICIPANT_INDEX = 0
GIT_REPO_KEY = "babykeyboard"
GIT_ROOT = os.environ.get("PYRRHULA_MCP_GIT_ROOT", "/app/data/blobs/repos")
BABYKEYBOARD_README = (
    "# babyKeyboard\n\nAn Electron desktop app: a baby-safe keyboard playground that does NOT "
    "exit on the browser-style keystrokes that normally end play (Esc, /, the Win/Super key, "
    "F-keys, Alt+Tab). Runs fullscreen/kiosk and swallows OS shortcuts.\n"
)

SWE_WORK_ITEMS = [
    (
        "Electron app scaffold",
        "Set up the Electron main + renderer, a fullscreen BrowserWindow, "
        "and npm scripts to run it.",
    ),
    (
        "Global keystroke guard",
        "Intercept and swallow Esc, '/', the Win/Super key, F-keys and "
        "Alt+Tab so they don't end play or leave the app.",
    ),
    (
        "On-screen keyboard UI",
        "Render a big, colourful on-screen keyboard that reacts to key "
        "presses with sounds and colours.",
    ),
]


async def _get_owner_principal(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        pid = await session.scalar(
            text(
                "SELECT id FROM principal WHERE tenant_id=:t AND kind='human' "
                "ORDER BY created_at LIMIT 1"
            ),
            {"t": tenant_id},
        )
    if pid is None:
        raise SystemExit("no human owner principal for this tenant")
    return pid


async def _ensure_owner_password(tenant_id: uuid.UUID, principal_id: uuid.UUID) -> str | None:
    """Set the owner's local-identity password to VERIFY_PASSWORD so the harness can log in.
    Returns the login email, or None if the owner has no local identity."""
    from argon2 import PasswordHasher

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT email FROM identity WHERE principal_id=:p AND provider='local' LIMIT 1"
                ),
                {"p": principal_id},
            )
        ).first()
        if row is None:
            return None
        email = row[0]
        await session.execute(
            text("UPDATE identity SET password_hash=:h WHERE principal_id=:p AND provider='local'"),
            {"h": PasswordHasher().hash(VERIFY_PASSWORD), "p": principal_id},
        )
    return email


async def _create_verify_tenant(slug: str) -> uuid.UUID:
    """A fresh deployment has no verify tenants -- create one with an owner whose login
    matches the harness's LOGIN table (same provisioning path signup uses)."""
    from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
    from core.tenancy.provisioning import create_tenant, create_tenant_user

    tenant_id, _workspace_id = await create_tenant(f"Verify {slug}", slug)
    principal_id = await create_tenant_user(tenant_id, f"{slug} owner", "owner")
    email = {
        "exec": "exec@pyrrhula.app",
        "rpg": "rpg@pyrrhula.com",
        "swe": "swe@pyrrhula.app",
    }.get(slug, f"{slug}@pyrrhula.app")
    await LocalArgon2IdentityProvider().register_local(
        tenant_id, principal_id, email, VERIFY_PASSWORD
    )
    print(f"  created tenant {slug!r} (owner {email})")
    return tenant_id


async def _tenant_and_workspace(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with unscoped_session() as session:
        tid = await session.scalar(text("SELECT id FROM tenant WHERE slug=:s"), {"s": slug})
    if tid is None:
        tid = await _create_verify_tenant(slug)
    async with tenant_scope(tid) as session:
        wid = await session.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))
    if wid is None:
        raise SystemExit(f"tenant {slug!r} has no workspace")
    return tid, wid


async def _ensure_connection(tenant_id: uuid.UUID, name: str, model: str) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(select(Agent.id).where(Agent.name == name))
    if existing is not None:
        return existing
    from api.encryptor_factory import get_encryptor

    agent = await create_agent(
        tenant_id, name, "ollama", model, api_base=OLLAMA_BASE, encryptor=get_encryptor()
    )
    return agent.id


async def _ensure_persona(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: str,
    name: str,
    agent_id: uuid.UUID,
    persona_type: str,
    persona_md: str,
) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(Persona).where(Persona.workspace_id == workspace_id, Persona.key == key)
        )
        if existing is not None:
            # Converge the connection (a re-seed may point a persona at a different model).
            if existing.agent_id != agent_id:
                existing.agent_id = agent_id
                await session.flush()
            return existing.id
    persona = await create_persona(
        tenant_id,
        workspace_id,
        key,
        name,
        agent_id,
        persona_type=persona_type,
        persona_md=persona_md,
    )
    return persona.id


async def _ensure_membership(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID, role: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if existing is not None:
            return
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role=role,
            )
        )
        await session.flush()


def _rpg_adventure_flow() -> dict[str, object]:
    from core.process.dsl.rpg_adventure import RPG_ADVENTURE_FLOW

    return RPG_ADVENTURE_FLOW


async def _ensure_definition(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    owner: uuid.UUID,
    key: str,
    definition: dict[str, object],
) -> uuid.UUID:
    from core.process.authoring import list_definitions

    for row in await list_definitions(tenant_id):
        if row.key == key:
            return row.id
    row = await create_definition(
        tenant_id,
        key,
        str(definition["name"]),
        definition,
        workspace_id=workspace_id,
        created_by=owner,
    )
    return row.id


async def _ensure_swe_workflow(tenant_id: uuid.UUID) -> None:
    """The moddable-workflow path in the harness: a tenant workflow cloned from the swdev
    template with repo access, selected as current (so sessions may pin repos)."""
    from core.workflows.service import (
        create_workflow,
        get_tenant_workflow_key,
        get_workflow_for_tenant,
        set_tenant_workflow,
    )

    existing = await get_workflow_for_tenant(tenant_id, "swe-delivery")
    if existing is None or existing.tenant_id is None:
        await create_workflow(
            tenant_id,
            "swe-delivery",
            "SWE Delivery",
            clone_from="swdev",
            repo_access=True,
        )
    if await get_tenant_workflow_key(tenant_id) != "swe-delivery":
        await set_tenant_workflow(tenant_id, "swe-delivery")


async def _ensure_babykb_repo(tenant_id: uuid.UUID) -> uuid.UUID:
    """The registry path: a `babykb` repo row (node20 runtime + a real test command) whose
    hosted store repo is imported from a podman-cp'd babyKeyboard source when present
    (file:// -- offline), else created empty."""
    from adapters.mcp.git_store import GitStore, GitStoreError
    from core.repos.service import create_repo, list_repos
    from core.repos.service import store_key as repo_store_key

    existing = {r.key: r for r in await list_repos(tenant_id)}
    if "babykb" in existing:
        repo = existing["babykb"]
    else:
        repo = await create_repo(
            tenant_id,
            "babykb",
            "babyKeyboard",
            description="Baby-safe Electron keyboard app",
            runtime="node20",
            test_cmd="node --version && test -f README.md",
        )
    store = GitStore(GIT_ROOT)
    skey = repo_store_key(tenant_id, "babykb")
    if os.path.isdir("/tmp/babykb-src"):
        try:
            await store.clone_from(skey, "file:///tmp/babykb-src")
        except GitStoreError:
            await store.ensure_repo(skey, seed_files={"README.md": BABYKEYBOARD_README})
    else:
        await store.ensure_repo(skey, seed_files={"README.md": BABYKEYBOARD_README})
    print(f"  repo={repo.id}")
    print(f"  store_key={skey}")
    return repo.id


async def _ensure_work_items(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> list[uuid.UUID]:
    """Create the swe work items (idempotent by key) and put them in 'in_progress' so a
    delegation's submit_for_review transition (in_progress -> in_review) applies."""
    from core.entities.repo import get_latest_schema_version
    from core.entities.storage import EntityRow, create_entity

    schema = await get_latest_schema_version(tenant_id, workspace_id, "work_item")
    if schema is None:
        schema = await get_latest_schema_version(tenant_id, None, "work_item")
    if schema is None:
        raise SystemExit("work_item schema missing (load the swdev pack)")
    definition = schema.to_definition()

    ids: list[uuid.UUID] = []
    for i, (title, desc) in enumerate(SWE_WORK_ITEMS):
        key = f"wi-{i + 1}"
        async with tenant_scope(tenant_id) as session:
            existing = await session.scalar(
                select(EntityRow.id).where(
                    EntityRow.workspace_id == workspace_id, EntityRow.key == key
                )
            )
        if existing is not None:
            # Converge state, not just existence: an earlier run may have left the item
            # anywhere in its lifecycle (in_review, approved, changes_requested...) --
            # each verification starts from in_progress (setup, not a game action).
            async with tenant_scope(tenant_id) as session:
                e = await session.get(EntityRow, existing)
                if e is not None and e.fsm_states.get("lifecycle") != "in_progress":
                    e.fsm_states = {"lifecycle": "in_progress"}
                    await session.flush()
            ids.append(existing)
            continue
        row = await create_entity(
            tenant_id,
            workspace_id,
            schema.id,
            definition,
            key,
            title,
            "workspace_public",
            {
                "title": title,
                "description": desc,
                "original_estimate": 3,
                "remaining_estimate": 3,
                "story_points": 3,
                "labels": [],
            },
        )
        # Seed straight into 'in_progress' (setup, not a game action -> set state directly).
        async with tenant_scope(tenant_id) as session:
            e = await session.get(EntityRow, row.id)
            e.fsm_states = {"lifecycle": "in_progress"}
            await session.flush()
        ids.append(row.id)
    return ids


async def _grant_persona_memberships(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, personas: list[tuple[uuid.UUID, str]]
) -> None:
    """Give each agent persona a workspace role so it can act on entities (entity:create /
    entity:mutate are gated on facilitator/participant membership; agent personas otherwise
    have none). Supervisor -> facilitator, participant -> participant."""
    async with tenant_scope(tenant_id) as session:
        for persona_id, persona_type in personas:
            principal_id = await session.scalar(
                select(Persona.principal_id).where(Persona.id == persona_id)
            )
            if principal_id is None:
                continue
            role = "facilitator" if persona_type == "supervisor" else "participant"
            await _ensure_membership(tenant_id, workspace_id, principal_id, role)


async def _load_pack_for(tenant_id: uuid.UUID, workspace_id: uuid.UUID, slug: str) -> None:
    """Materialize a pack's schemas / rule systems / tools / processes into the tenant
    (idempotent). Gives rpg its character schema (health FSM) + coin_flip rule system."""
    if slug not in PACK_DIRS:
        return
    import pathlib

    from core.packs.loader import load_pack

    await load_pack(pathlib.Path("/app") / PACK_DIRS[slug], tenant_id, workspace_id)


async def _ensure_roundtable(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    owner: uuid.UUID,
    phase_prompts: dict[str, str] | None = None,
) -> uuid.UUID:
    """Publish a fast single-round variant of the domain-neutral round table for the
    verification scenario (a redeploy smoke test wants one round -> synthesis, not three).
    Distinct key from any user-facing flow so it never shadows one."""
    import copy

    from core.process.authoring import list_definitions

    key = "verify_round_table"
    definition = copy.deepcopy(AGENT_ROUND_TABLE_FLOW)
    definition["name"] = "Verification Round Table"
    definition["state"]["max_rounds"]["default"] = 1  # type: ignore[index]
    for phase_key, prompt in (phase_prompts or {}).items():
        definition["phases"][phase_key]["prompt"] = prompt  # type: ignore[index]

    existing = [row for row in await list_definitions(tenant_id) if row.key == key]
    if existing:
        latest = max(existing, key=lambda r: r.version)
        if latest.definition == definition:
            return latest.id
        # Content drifted (e.g. new phase prompts): definitions are immutable, so a new
        # version is created and the harness pins that.
    row = await create_definition(
        tenant_id,
        key,
        str(definition["name"]),
        definition,
        workspace_id=workspace_id,
        created_by=owner,
    )
    return row.id


# What each phase of the swe flow is FOR, in software terms -- the structure the
# facilitator's output was missing (UI feedback: "barely structured for human
# consumption").
SWE_PHASE_PROMPTS = {
    "framing": (
        "You are opening a software delivery session. Output structured markdown, no "
        "conversational filler: '## Objective' (one sentence, from the agenda), "
        "'## Work breakdown' (a numbered list mapping the known work items to what each "
        "must deliver), '## Delegation plan' (who builds what, what done means -- CI "
        "green and review approved), '## Risks' (max 3 bullets)."
    ),
    "discussion": (
        "You are a developer weighing in on the plan. Markdown only: '### Assessment' "
        "(does the breakdown cover the requirements? anything missing or mis-scoped?) "
        "and '### Proposal' (the concrete approach you'd take for your part: files, "
        "libraries, test strategy). Under 200 words, no filler."
    ),
    "regroup": (
        "Regroup as the tech lead: '## Decisions' (bullets: what the team settled), "
        "'## Adjustments' (changes to the work breakdown, if any), '## Ready to "
        "delegate' (the work items now ready, one line each). Markdown, no filler."
    ),
    "synthesis": (
        "Close the planning stage with a delivery brief in markdown: '## Objective', "
        "'## Final work breakdown' (numbered, one line per work item: scope + acceptance "
        "criteria), '## Delegation order', '## Review policy' (what you will check on "
        "each PR). This document is what the coding agents will be held to."
    ),
}


async def seed_tenant(slug: str) -> None:
    if slug not in ROSTERS:
        raise SystemExit(f"no roster defined for {slug!r} (have: {', '.join(ROSTERS)})")
    supervisor_spec, participant_specs = ROSTERS[slug]
    tid, wid = await _tenant_and_workspace(slug)
    owner = await _get_owner_principal(tid)

    participant_conn = await _ensure_connection(tid, f"{slug}-small", PARTICIPANT_MODEL)
    supervisor_conn = await _ensure_connection(tid, f"{slug}-supervisor", SUPERVISOR_MODEL)
    # swe wants one strong participant + smaller ones (the strong model is only worth its
    # latency here). The delegated coding step is a scaffold today, so this shapes the
    # discussion, not the PR output -- wired for when real codegen lands.
    strong_conn = (
        await _ensure_connection(tid, f"{slug}-strong", STRONG_MODEL) if slug == "swe" else None
    )
    # The swe Lead plans the delivery AND reviews the PRs -- both are coding judgment,
    # which the small instruction-follower is visibly not enough for (UI feedback).
    if strong_conn is not None:
        supervisor_conn = strong_conn

    sup_key = f"{slug}-{supervisor_spec[0].lower()}"
    sup_id = await _ensure_persona(
        tid, wid, sup_key, supervisor_spec[0], supervisor_conn, "supervisor", supervisor_spec[1]
    )
    participant_ids: list[uuid.UUID] = []
    for i, (name, md) in enumerate(participant_specs):
        conn = (
            strong_conn
            if (strong_conn is not None and i == STRONG_PARTICIPANT_INDEX)
            else participant_conn
        )
        pid = await _ensure_persona(
            tid, wid, f"{slug}-{name.lower()}", name, conn, "participant", md
        )
        participant_ids.append(pid)

    await _ensure_membership(tid, wid, owner, "overseer")
    def_id = await _ensure_roundtable(
        tid, wid, owner, phase_prompts=SWE_PHASE_PROMPTS if slug == "swe" else None
    )
    email = await _ensure_owner_password(tid, owner)

    # rpg/swe: load the pack (schemas, rule systems, ...) and give the agent personas a
    # workspace role so they may create/mutate entities.
    await _load_pack_for(tid, wid, slug)
    persona_types = [(sup_id, "supervisor")] + [(p, "participant") for p in participant_ids]
    await _grant_persona_memberships(tid, wid, persona_types)
    if slug == "rpg":
        await _ensure_definition(tid, wid, owner, "rpg_adventure", _rpg_adventure_flow())
    work_item_ids: list[uuid.UUID] = []
    if slug == "swe":
        await _ensure_swe_workflow(tid)
        await _ensure_babykb_repo(tid)
        work_item_ids = await _ensure_work_items(tid, wid)

    print(f"tenant={slug} workspace={wid}")
    if work_item_ids:
        print(f"  work_items={','.join(str(w) for w in work_item_ids)}")
    print(f"  login={email} password={VERIFY_PASSWORD}")
    print(f"  definition={def_id}")
    print(f"  supervisor={sup_id}")
    print(f"  participants={','.join(str(p) for p in participant_ids)}")


async def _main(slugs: list[str]) -> None:
    for slug in slugs:
        await seed_tenant(slug)


if __name__ == "__main__":
    targets = sys.argv[1:] or ["exec"]
    asyncio.run(_main(targets))
