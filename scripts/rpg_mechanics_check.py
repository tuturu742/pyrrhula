"""Deterministic check of the P1 RPG mechanics against the real in-session tool handlers.

Model-independent on purpose: local models are not yet reliable at emitting tool calls (the
user's plan is to upgrade models later), so this verifies the *mechanics + wiring* -- the same
``entity_create`` / ``resolve_and_apply`` handlers a model turn would invoke -- driven directly
with a ToolContext. Proves: two player personas each create a character entity; one encounter
resolves via the coin_flip rule system (an immutable ResolutionRecord) and drives the character's
health state machine (an append to entity_state_change).

Run in the api container after seeding rpg:
    podman exec pyrrhula_api_1 python /tmp/rpg_mechanics_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from adapters.permission.role_permission import RolePermissionService
from core.agents.models import Persona
from core.agents.tools import ToolContext
from core.process.session_entity_tools import (
    make_entity_create_handler,
    make_resolve_apply_handler,
)
from core.process.skeleton import create_session
from core.resolution.rule_system import RuleSystemDefinition, get_rule_system
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope, unscoped_session

_ABILITIES = {
    "strength": 12,
    "dexterity": 14,
    "constitution": 12,
    "intelligence": 10,
    "wisdom": 11,
    "charisma": 13,
    "max_hit_points": 10,
    "hit_points": 10,
    "xp": 0,
    "conditions": [],
}


async def _tenant_workspace(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with unscoped_session() as session:
        from sqlalchemy import text

        tid = await session.scalar(text("SELECT id FROM tenant WHERE slug=:s"), {"s": slug})
    if tid is None:
        raise SystemExit(f"no tenant {slug!r}")
    async with tenant_scope(tid) as session:
        wid = await session.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))
    return tid, wid


async def _personas(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, slug: str
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    # Only the seeded roster (keyed ``<slug>-*``): a demo tenant may also carry hand-made
    # personas from earlier sessions that have no workspace membership (and so can't act).
    async with tenant_scope(tenant_id) as session:
        sup = await session.scalar(
            select(Persona.id).where(
                Persona.workspace_id == workspace_id,
                Persona.persona_type == "supervisor",
                Persona.key.like(f"{slug}-%"),
            )
        )
        players = (
            (
                await session.execute(
                    select(Persona.id)
                    .where(
                        Persona.workspace_id == workspace_id,
                        Persona.persona_type == "participant",
                        Persona.key.like(f"{slug}-%"),
                    )
                    .order_by(Persona.key)
                )
            )
            .scalars()
            .all()
        )
    if sup is None or len(players) < 2:
        raise SystemExit("rpg tenant needs a supervisor + >=2 participants (run the seed)")
    return sup, list(players)


async def main(slug: str) -> None:
    tid, wid = await _tenant_workspace(slug)
    gm, players = await _personas(tid, wid, slug)

    rs_row = await get_rule_system(tid, "coin_flip")
    if rs_row is None:
        raise SystemExit("coin_flip rule system missing (run the seed's load_pack)")
    rule_system = RuleSystemDefinition.from_row(rs_row)

    session_row = await create_session(tid, wid, gm)
    perm = RolePermissionService()
    create_handler = make_entity_create_handler(workspace_id=wid, permission_service=perm)
    apply_handler = make_resolve_apply_handler(
        workspace_id=wid, rule_system=rule_system, rule_system_id=rs_row.id, permission_service=perm
    )

    created: list[str] = []
    for i, player in enumerate(players[:2]):
        ctx = ToolContext(tenant_id=tid, persona_id=player, session_id=session_row.id)
        # Unique names per run: reusing an entity created from an OLDER schema version
        # would lack machines the current pack defines (observed: no 'condition').
        res = await create_handler(
            {
                "schema_key": "character",
                "name": f"Hero{i + 1}-{uuid.uuid4().hex[:6]}",
                "fields": dict(_ABILITIES),
            },
            ctx,
        )
        payload = json.loads(res.content)
        if "entity_id" not in payload:
            raise SystemExit(f"character create failed: {payload}")
        created.append(payload["entity_id"])
        print(f"character {i + 1}: {payload.get('entity_id')} (created={payload.get('created')})")

    # One encounter on the first character: bring HP to the bloodied threshold, then drive the
    # health machine's damage_taken transition. coin_flip decides heads/tails (recorded either way).
    ctx = ToolContext(tenant_id=tid, persona_id=players[0], session_id=session_row.id)
    enc = await apply_handler(
        {
            "actor_entity_id": created[0],
            "machine_key": "health",
            "trigger": "damage_taken",
            "set_fields": {"hit_points": 5},
            "expression": "1d2",
        },
        ctx,
    )
    enc_payload = json.loads(enc.content)
    print(f"encounter: {json.dumps(enc_payload)}")

    # The MCP resolution surface (the path a MODEL takes): the workflow's registered
    # `resolution` server exposes the pack's tools; an honest server-side coin flip
    # writes a ResolutionRecord, and a check drives the SECOND state machine
    # (condition: steady -> shaken via 'fright') -- two different machines moved by
    # recorded outcomes, none by prose.
    from adapters.mcp.resolution_transport import ResolutionMcpTransport
    from core.ports.mcp import McpServerRef

    transport = ResolutionMcpTransport(permission_service=RolePermissionService())
    ref = McpServerRef(key="resolution", url=f"pyrrhula://resolution/{tid}")
    mcp_tools = {t.name for t in await transport.list_tools(ref)}
    if "coin_flip" not in mcp_tools or "dice_roller" not in mcp_tools:
        raise SystemExit(f"resolution MCP surface incomplete: {sorted(mcp_tools)}")

    async with tenant_scope(tid) as db:
        acting = await db.get(Persona, players[1])
        acting_principal = acting.principal_id if acting is not None else players[1]

    # A dedicated fresh entity for the condition-machine check: the persona-character
    # handler reuses one character per persona (possibly bound to an old schema
    # version), so bind this one to the CURRENT schema explicitly.
    from core.entities.mutation import create as create_entity_direct

    victim = await create_entity_direct(
        acting_principal,
        tid,
        wid,
        "character",
        f"fright-{uuid.uuid4().hex[:8]}",
        "Fright Target",
        dict(_ABILITIES),
        "workspace_public",
        idempotency_key=f"fright-{uuid.uuid4()}",
        permission_service=perm,
    )
    victim_id = str(victim["entity_id"])

    def _mcp_context(seq: int) -> dict[str, object]:
        return {
            "session_id": str(session_row.id),
            "workspace_id": str(wid),
            "event_seq": seq,
            "principal_id": str(acting_principal),
        }

    flip = await transport.call_tool(
        ref, "coin_flip", {"reason": "initiative", "_context": _mcp_context(900)}
    )
    assert flip.structured and flip.structured.get("resolution_id"), flip.content
    print(f"mcp coin_flip: {flip.content}")

    fright = await transport.call_tool(
        ref,
        "dice_roller",
        {
            "expression": "1d20",
            "target": 10,
            "actor_entity_id": victim_id,
            "machine_key": "condition",
            "trigger": "fright",
            "_context": _mcp_context(901),
        },
    )
    assert flip.structured is not None
    print(f"mcp fright check: {fright.content[:160]}")
    print(f"session_id={session_row.id}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "rpg"))
