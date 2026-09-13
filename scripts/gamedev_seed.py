"""Gamedev verification seed (run inside the api pod: `python - gamedev` via API_EXEC).

Idempotent provisioning of the runbook's gamedev scenario: tenant `gamedev` on the
swdev workflow with tenant-level MCP grants (engine/assets sidecars), a Godot project
repo (headless tests + real Web export build), a supervisor persona, one work item in
`in_progress`, and a deterministic owner login. Prints `key=value` lines for the
host-side driver (scripts/verify_deploy.py scenario_gamedev).

Env knobs (all optional): PYRRHULA_VERIFY_GODOT_MCP / PYRRHULA_VERIFY_COMFY_MCP
(default the k8s sidecar Services), PYRRHULA_VERIFY_GAMEDEV_MODEL (default
ollama/qwen3.8:27b at http://ollama:11434).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from argon2 import PasswordHasher
from sqlalchemy import select, text

from adapters.mcp.git_store import GitStore, default_git_root
from api.encryptor_factory import get_encryptor
from core.agents.authoring import create_agent, create_persona
from core.agents.models import Agent, Persona
from core.entities.repo import get_latest_schema_version
from core.entities.storage import EntityRow, create_entity
from core.mcp.registry import upsert_tenant_capability
from core.repos.service import create_repo, store_key
from core.tenancy.models import Membership, Principal, Workspace, WorkspaceMembership
from core.tenancy.provisioning import create_tenant
from core.tenancy.scope import tenant_scope, unscoped_session
from core.workflows.service import apply_workflow_capabilities, set_tenant_workflow

OWNER_EMAIL = "gamedev@pyrrhula.app"
VERIFY_PASSWORD = "verify-pass-123"  # same convention as scripts/seed_verification.py

GODOT_MCP = os.environ.get("PYRRHULA_VERIFY_GODOT_MCP", "http://godot-mcp:8090/mcp")
COMFY_MCP = os.environ.get("PYRRHULA_VERIFY_COMFY_MCP", "http://comfy-mcp:8091/mcp")
MODEL = os.environ.get("PYRRHULA_VERIFY_GAMEDEV_MODEL", "qwen3.8:27b")
OLLAMA = os.environ.get("PYRRHULA_VERIFY_OLLAMA", "http://ollama:11434")

SKELETON = {
    "project.godot": (
        "; Engine configuration. Godot 4.x project.\n"
        'config_version=5\n\n[application]\nconfig/name="VGame"\n'
        'run/main_scene="res://main.tscn"\n\n[rendering]\n'
        'renderer/rendering_method="gl_compatibility"\n'
    ),
    "main.tscn": (
        "[gd_scene load_steps=2 format=3]\n\n"
        '[ext_resource type="Script" path="res://scripts/game.gd" id="1"]\n\n'
        '[node name="Main" type="Node2D"]\nscript = ExtResource("1")\n'
    ),
    "scripts/game.gd": (
        "extends Node2D\n\nconst MAX_AIR := 100\n\n"
        'func _ready() -> void:\n\tprint("VGame booted")\n\n'
        "static func air_after(seconds_underwater: int, has_rebreather: bool) -> int:\n"
        "\tvar burn_rate := 1 if has_rebreather else 2\n"
        "\treturn clampi(MAX_AIR - seconds_underwater * burn_rate, 0, MAX_AIR)\n"
    ),
    "tests/run_tests.gd": (
        'extends SceneTree\n\nconst Game = preload("res://scripts/game.gd")\n\n'
        "func _initialize() -> void:\n"
        "\tvar failures := 0\n"
        "\tfailures += 0 if Game.air_after(0, false) == 100 else 1\n"
        "\tfailures += 0 if Game.air_after(10, true) == 90 else 1\n"
        '\tprint("ALL TESTS PASSED" if failures == 0 else "FAILED")\n'
        "\tquit(1 if failures > 0 else 0)\n"
    ),
    ".gitignore": ".godot/\nbuild/\ngame-web.tar.gz\n",
    "export_presets.cfg": (
        '[preset.0]\n\nname="Web"\nplatform="Web"\nrunnable=true\n'
        'advanced_options=false\ndedicated_server=false\ncustom_features=""\n'
        'export_filter="all_resources"\ninclude_filter=""\nexclude_filter=""\n'
        'export_path="build/web/index.html"\nencryption_include_filters=""\n'
        'encryption_exclude_filters=""\nencrypt_pck=false\nencrypt_directory=false\n\n'
        '[preset.0.options]\n\ncustom_template/debug=""\ncustom_template/release=""\n'
        "variant/extensions_support=false\nvram_texture_compression/for_desktop=true\n"
        "vram_texture_compression/for_mobile=false\nhtml/export_icon=true\n"
        'html/custom_html_shell=""\nhtml/head_include=""\n'
        "html/canvas_resize_policy=2\nhtml/focus_canvas_on_start=true\n"
        "html/experimental_virtual_keyboard=false\nprogressive_web_app/enabled=false\n"
    ),
}

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


async def _ensure_tenant(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with unscoped_session() as session:
        tid = await session.scalar(text("SELECT id FROM tenant WHERE slug=:s"), {"s": slug})
    if tid is None:
        tid, wid = await create_tenant("GameDev Verify", slug)
        return tid, wid
    async with tenant_scope(tid) as session:
        wid = await session.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))
    return tid, wid


async def _ensure_owner(tid: uuid.UUID, wid: uuid.UUID) -> uuid.UUID:
    from core.tenancy.models import Identity

    async with tenant_scope(tid) as session:
        principal_id = await session.scalar(
            select(Identity.principal_id).where(
                Identity.provider == "local", Identity.external_id == OWNER_EMAIL
            )
        )
        if principal_id is None:
            principal = Principal(tenant_id=tid, kind="human", display_name="GameDev Owner")
            session.add(principal)
            await session.flush()
            principal_id = principal.id
            session.add(Membership(tenant_id=tid, principal_id=principal_id, role="owner"))
            session.add(
                Identity(
                    tenant_id=tid,
                    principal_id=principal_id,
                    provider="local",
                    external_id=OWNER_EMAIL,
                    password_hash=PasswordHasher().hash(VERIFY_PASSWORD),
                )
            )
        else:
            await session.execute(
                text(
                    "UPDATE identity SET password_hash=:h WHERE provider='local' AND external_id=:e"
                ),
                {"h": PasswordHasher().hash(VERIFY_PASSWORD), "e": OWNER_EMAIL},
            )
        member = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == wid,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if member is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tid, workspace_id=wid, principal_id=principal_id, role="overseer"
                )
            )
    return principal_id


async def _ensure_persona(
    tid: uuid.UUID,
    wid: uuid.UUID,
    key: str = "gv-lead",
    name: str = "Lead",
    persona_type: str = "supervisor",
) -> uuid.UUID:
    async with tenant_scope(tid) as session:
        agent_id = await session.scalar(
            select(Agent.id).where(Agent.name == "gv-model", Agent.archived_at.is_(None))
        )
    if agent_id is None:
        agent = await create_agent(
            tid, "gv-model", "ollama", MODEL, api_base=OLLAMA, encryptor=get_encryptor()
        )
        agent_id = agent.id
    async with tenant_scope(tid) as session:
        persona = await session.scalar(
            select(Persona).where(
                Persona.workspace_id == wid,
                Persona.key == key,
                Persona.archived_at.is_(None),
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
            persona_md="You are on a tiny game team. Keep replies short.",
        )
    async with tenant_scope(tid) as session:
        member = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == wid,
                WorkspaceMembership.principal_id == persona.principal_id,
            )
        )
        if member is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tid,
                    workspace_id=wid,
                    principal_id=persona.principal_id,
                    role="facilitator" if persona_type == "supervisor" else "participant",
                )
            )
    return persona.id


async def _ensure_repo(tid: uuid.UUID) -> uuid.UUID:
    from core.repos.models import RepoRow

    async with tenant_scope(tid) as session:
        repo_id = await session.scalar(
            select(RepoRow.id).where(RepoRow.key == "vgame", RepoRow.archived_at.is_(None))
        )
        if repo_id is not None:
            row = await session.get(RepoRow, repo_id)
            row.test_cmd, row.build_cmd, row.artifact_name = (
                TEST_CMD,
                BUILD_CMD,
                "game-web.tar.gz",
            )
            return repo_id
    row = await create_repo(
        tid,
        "vgame",
        "VGame",
        runtime="custom",
        runtime_image="docker.io/barichello/godot-ci:4.3",
        test_cmd=TEST_CMD,
        build_cmd=BUILD_CMD,
        artifact_name="game-web.tar.gz",
    )
    return row.id


async def _ensure_work_item(tid: uuid.UUID, wid: uuid.UUID) -> uuid.UUID:
    schema = await get_latest_schema_version(tid, wid, "work_item") or (
        await get_latest_schema_version(tid, None, "work_item")
    )
    if schema is None:
        raise SystemExit("work_item schema missing (swdev pack not loaded)")
    async with tenant_scope(tid) as session:
        existing = await session.scalar(
            select(EntityRow.id).where(EntityRow.workspace_id == wid, EntityRow.key == "gv-wi-1")
        )
    if existing is None:
        row = await create_entity(
            tid,
            wid,
            schema.id,
            schema.to_definition(),
            "gv-wi-1",
            "Rebreather efficiency",
            "workspace_public",
            {
                "title": "Rebreather efficiency",
                "description": (
                    "Add static func rebreather_bonus(level: int) -> int to "
                    "scripts/game.gd returning clampi(level * 2, 0, 20), and one "
                    "test for it in tests/run_tests.gd."
                ),
                "original_estimate": 1,
                "remaining_estimate": 1,
                "story_points": 1,
                "labels": [],
            },
        )
        existing = row.id
    async with tenant_scope(tid) as session:
        entity = await session.get(EntityRow, existing)
        entity.fsm_states = {"lifecycle": "in_progress"}
    return existing


async def main(slug: str) -> None:
    tid, wid = await _ensure_tenant(slug)
    await set_tenant_workflow(tid, "swdev")
    await upsert_tenant_capability(
        tid,
        "engine",
        GODOT_MCP,
        enabled_tools=["run_gdscript", "godot_version"],
        require_confirmation=False,
    )
    await upsert_tenant_capability(
        tid,
        "assets",
        COMFY_MCP,
        enabled_tools=["generate_image"],
        effectful_tools=["generate_image"],
        require_confirmation=False,
    )
    applied = await apply_workflow_capabilities(tid, wid)
    await _ensure_owner(tid, wid)
    persona_id = await _ensure_persona(tid, wid)
    dev_id = await _ensure_persona(tid, wid, key="gv-dev", name="Dev", persona_type="participant")
    repo_id = await _ensure_repo(tid)
    store = GitStore(default_git_root())
    await store.ensure_repo(store_key(tid, "vgame"))
    await store.import_tree(store_key(tid, "vgame"), SKELETON)
    work_item = await _ensure_work_item(tid, wid)

    # A definition to pin sessions to (any swdev-pack flow present for the tenant).
    async with tenant_scope(tid) as session:
        definition_id = await session.scalar(
            text(
                "SELECT id FROM process_definition WHERE tenant_id=:t ORDER BY created_at LIMIT 1"
            ),
            {"t": tid},
        )

    print(f"tenant={tid}")
    print(f"workspace={wid}")
    print(f"login={OWNER_EMAIL} password={VERIFY_PASSWORD}")
    print(f"supervisor={persona_id}")
    print(f"participant={dev_id}")
    print(f"repo={repo_id}")
    print(f"store_key={store_key(tid, 'vgame')}")
    print(f"work_item={work_item}")
    print(f"definition={definition_id}")
    print(f"capabilities={','.join(sorted(set(applied)))}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "gamedev"))
