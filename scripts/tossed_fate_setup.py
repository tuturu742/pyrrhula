"""TossedFate showcase setup (run inside the api pod: kubectl exec -i ... python - <slug>).

Idempotent per-tenant provisioning on top of an rpg-workflow tenant: the TossedFate
character schema (SIMPLE attributes, depletable luck, freeform skills, a wounds
machine per health-and-combat.md's narrative state machine), one model connection per
table role, and a GM + three player personas with acting workspace memberships.
Prints ids as key=value for the host-side driver.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from sqlalchemy import select, text

from api.encryptor_factory import get_encryptor
from core.agents.authoring import create_agent, create_persona
from core.agents.models import Agent, Persona
from core.entities.repo import next_version, save_schema
from core.entities.schema import EntitySchemaDefinition
from core.tenancy.models import Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope, unscoped_session

OLLAMA = "http://ollama:11434"

SCHEMA = {
    "fields": [
        {"key": "strength", "type": "integer", "minimum": 0, "maximum": 10, "tags": ["attribute"]},
        {
            "key": "intelligence",
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "tags": ["attribute"],
        },
        {"key": "mobility", "type": "integer", "minimum": 0, "maximum": 10, "tags": ["attribute"]},
        {
            "key": "perception",
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "tags": ["attribute"],
        },
        {"key": "empathy", "type": "integer", "minimum": 0, "maximum": 10, "tags": ["attribute"]},
        {"key": "luck_max", "type": "integer", "minimum": 0, "maximum": 10},
        {"key": "luck_current", "type": "integer", "minimum": 0, "maximum": 10},
        {"key": "race", "type": "string"},
        {"key": "class_name", "type": "string"},
        {"key": "skills", "type": "array", "items": "string"},
        {"key": "base_attack", "type": "integer", "minimum": 0, "maximum": 10},
        {"key": "base_defence", "type": "integer", "minimum": 0, "maximum": 10},
    ],
    "derived": [],
    "constraints": [
        {
            "expression": "fields.luck_current <= fields.luck_max",
            "message": "luck_current cannot exceed luck_max",
        },
    ],
    "state_machines": [
        {
            "key": "wounds",
            "states": [
                {"key": "unharmed", "label_key": "wounds.unharmed"},
                {"key": "wounded", "label_key": "wounds.wounded"},
                {"key": "down", "label_key": "wounds.down"},
            ],
            "initial": "unharmed",
            "transitions": [
                {"from": "unharmed", "to": "wounded", "trigger": "hit"},
                {"from": "wounded", "to": "down", "trigger": "hit"},
                {"from": "wounded", "to": "unharmed", "trigger": "rest"},
                {"from": "down", "to": "wounded", "trigger": "rest"},
            ],
        }
    ],
    "views": [],
}

ROSTER = [
    (
        "tf-gm",
        "Game Master",
        "supervisor",
        "qwen3.8:27b",
        "You are the Game Master of a TossedFate one-shot in the world of Sydenus. "
        "TossedFate resolves EVERYTHING by coin pools: pool = Complexity + Attribute + "
        "Skill bonus + Luck spent; flip the pool with the dice_roller tool as "
        "expression '<pool>d2' and target '<pool> + Complexity' -- success means enough "
        "coins landed on the chosen face. NEVER invent flip results; always call the "
        "tool and narrate what the recorded outcome says. Guide character creation "
        "strictly by the rules (10-coin toss for attribute points via '10d2', target "
        "15), then run one short encounter.",
    ),
    (
        "tf-p1",
        "Wren",
        "participant",
        "hermes3:8b",
        "You play Wren, a nimble halfling scout in Sydenus. Follow the GM, create your "
        "character per the TossedFate rules using the entity tools, pick freeform "
        "skills that fit a scout, and let coin flips decide outcomes.",
    ),
    (
        "tf-p2",
        "Borga",
        "participant",
        "hermes3:8b",
        "You play Borga, a half-giant former mercenary with a soft heart. Create your "
        "character per the TossedFate rules with the entity tools; favour Strength; "
        "spend Luck boldly.",
    ),
    (
        "tf-p3",
        "Liss",
        "participant",
        "qwen2.5:7b",
        "You play Liss, a curious scholar of First Era ruins. Create your character "
        "per the TossedFate rules with the entity tools; favour Intelligence and "
        "Perception; be cautious with Luck.",
    ),
]


async def main(slug: str) -> None:
    async with unscoped_session() as session:
        tid = await session.scalar(text("SELECT id FROM tenant WHERE slug=:s"), {"s": slug})
    if tid is None:
        raise SystemExit(f"no tenant {slug!r}")
    async with tenant_scope(tid) as session:
        wid = await session.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))
        owner = await session.scalar(
            text(
                "SELECT id FROM principal WHERE tenant_id=:t AND kind='human' "
                "ORDER BY created_at LIMIT 1"
            ),
            {"t": tid},
        )

    definition = EntitySchemaDefinition.model_validate(SCHEMA)
    version = await next_version(tid, wid, "tossed_fate_character")
    await save_schema(tid, wid, "tossed_fate_character", version, definition)
    print(f"schema=tossed_fate_character v{version}")

    # Owner needs an overseer membership to conduct sessions (not automatic).
    async with tenant_scope(tid) as session:
        existing = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == wid,
                WorkspaceMembership.principal_id == owner,
            )
        )
        if existing is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tid, workspace_id=wid, principal_id=owner, role="overseer"
                )
            )

    persona_ids: dict[str, uuid.UUID] = {}
    for key, name, ptype, model, persona_md in ROSTER:
        async with tenant_scope(tid) as session:
            agent_id = await session.scalar(
                select(Agent.id).where(Agent.name == f"tf-{model}", Agent.archived_at.is_(None))
            )
        if agent_id is None:
            agent = await create_agent(
                tid,
                f"tf-{model}",
                "ollama",
                model,
                api_base=OLLAMA,
                encryptor=get_encryptor(),
            )
            agent_id = agent.id
        async with tenant_scope(tid) as session:
            existing_persona = await session.scalar(
                select(Persona).where(
                    Persona.workspace_id == wid, Persona.key == key, Persona.archived_at.is_(None)
                )
            )
        if existing_persona is None:
            persona = await create_persona(
                tid, wid, key, name, agent_id, persona_type=ptype, persona_md=persona_md
            )
        else:
            persona = existing_persona
        persona_ids[key] = persona.id
        role = "facilitator" if ptype == "supervisor" else "participant"
        async with tenant_scope(tid) as session:
            has = await session.scalar(
                select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == wid,
                    WorkspaceMembership.principal_id == persona.principal_id,
                )
            )
            if has is None:
                session.add(
                    WorkspaceMembership(
                        tenant_id=tid,
                        workspace_id=wid,
                        principal_id=persona.principal_id,
                        role=role,
                    )
                )

    print(f"workspace={wid}")
    print(f"gm={persona_ids['tf-gm']}")
    print(f"players={persona_ids['tf-p1']},{persona_ids['tf-p2']},{persona_ids['tf-p3']}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "tossed-fate"))
