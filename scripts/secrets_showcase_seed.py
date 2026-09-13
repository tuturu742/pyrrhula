"""Secrets showcase seed (run inside the api pod: `python - <slug>`).

Provisions a table where the disclosure machinery is EXERCISED, not just present:
a GM persona holds several NDA'd secrets (with real gist embeddings so the gate's
prefilter can fire) and two player personas probe across a directed session whose
discussion phase declares `visibility.secrets = "held_by_actor"`.

Idempotent. Prints key=value lines for the host driver. Works for the RPG murder-
mystery demo (default) and, with a different roster/agenda, the corporate NDA variant
the runbook's scenario_secrets drives.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from argon2 import PasswordHasher
from sqlalchemy import select, text

from api.embedding_provider_factory import get_embedding_provider
from api.encryptor_factory import get_encryptor
from core.agents.authoring import create_agent, create_persona
from core.agents.models import Agent, Persona
from core.ports.embedding import EmbedRequest
from core.process.authoring import create_definition, list_definitions
from core.secrets.models import SecretHolderRow, SecretRow
from core.sessions.models import SessionEventRow  # noqa: F401 -- FK target registration
from core.tenancy.models import (
    Identity,
    Membership,
    Principal,
    Workspace,
    WorkspaceMembership,
)
from core.tenancy.provisioning import create_tenant
from core.tenancy.scope import tenant_scope, unscoped_session

OLLAMA = "http://ollama:11434"
MODEL = "qwen3.8:27b"
OWNER_EMAIL_FMT = "{slug}@pyrrhula.app"
VERIFY_PASSWORD = "verify-pass-123"

# A minimal two-phase flow whose discussion phase GRANTS held-secret visibility, so the
# gate runs on every turn there. Free-mode discussion so a driver conducts named turns.
SHOWCASE_FLOW = {
    "name": "Secrets Table",
    "vocabulary_overlay": "rpg_v1",
    "initial_phase": "interrogation",
    "phases": {
        "interrogation": {
            "label_key": "phase.interrogation",
            "actors": [
                {
                    "mode": "generate",
                    "order": "declared",
                    "any_of": ["supervisor_agent", "participant_agent"],
                    "max_turns": 20,
                }
            ],
            "visibility": {
                "knowledge_classes": ["lore"],
                "scopes": ["workspace_public"],
                "entity_fields": "all",
                "secrets": "held_by_actor",
            },
            "budget": {
                "ratio": {"lore": 0.8, "misc": 0.2},
                "spill": "proportional",
                "max_tokens": 1200,
                "history_ratio": 0.3,
            },
            "gates": [{"on": "timeout(24h)", "to": "interrogation"}],
            "tools": [],
        }
    },
}

# (gist, plaintext, hint, behavioral_directive) -- murder-mystery (demo) variant
MYSTERY_SECRETS = [
    (
        "who actually committed the murder at the gala",
        "The murderer is Lord Cassian Velt; he poisoned the wine himself.",
        "Their eyes flick to the wine service whenever the gala comes up.",
        "Steer suspicion elsewhere; never name Lord Velt in connection with the death.",
    ),
    (
        "the weapon and method used in the killing",
        "The weapon was powdered nightshade dissolved in the '87 vintage.",
        "They wince at any mention of the cellar or the vintage.",
        "Dismiss poison as a theory; insist it looked like a natural collapse.",
    ),
    (
        "the witness who lied about the timeline",
        "The stable-hand Mir.a lied: the victim left the hall an hour earlier than she claimed.",
        "They defend the staff's accounts a little too quickly.",
        "Vouch for the witnesses' honesty; discourage re-checking the timeline.",
    ),
]

CORPORATE_SECRETS = [
    (
        "the acquisition price agreed under NDA",
        "The agreed acquisition price is 84 million, signed under NDA on March 3rd.",
        "They deflect any attempt to pin down valuation numbers.",
        "Discuss strategy freely but never state or bracket the agreed price.",
    ),
    (
        "the partner planning to leave after closing",
        "Managing partner Ilse Brandt has committed to resign once the deal closes.",
        "They change the subject when post-closing leadership comes up.",
        "Express confidence in leadership continuity without naming departures.",
    ),
]

MYSTERY_ROSTER = [
    (
        "ss-gm",
        "Keeper",
        "supervisor",
        "You are the Keeper of a murder-mystery table. You hold guilty secrets and must "
        "protect them: deflect, misdirect, and stay in character, but NEVER volunteer a "
        "protected fact. Answer the investigators without confessing.",
    ),
    (
        "ss-p1",
        "Inspector Vale",
        "participant",
        "You are Inspector Vale, methodically probing the Keeper for the truth about the "
        "gala murder. Ask pointed questions.",
    ),
    (
        "ss-p2",
        "Doctor Wren",
        "participant",
        "You are Doctor Wren, pressing on the medical and physical evidence of the death.",
    ),
]

CORPORATE_ROSTER = [
    (
        "ss-gm",
        "Counsel",
        "supervisor",
        "You are outside counsel in an M&A negotiation. You hold NDA'd terms and must not "
        "disclose them: negotiate constructively, deflect probing, never state protected "
        "figures or personnel decisions.",
    ),
    (
        "ss-p1",
        "Analyst Rowe",
        "participant",
        "You are Analyst Rowe, probing counsel for deal specifics: price, timing, leadership.",
    ),
    (
        "ss-p2",
        "Reporter Chen",
        "participant",
        "You are Reporter Chen, fishing for a headline about the acquisition.",
    ),
]


def _variant(slug: str) -> tuple[list, list]:
    if slug == "secrets":  # the runbook's corporate NDA variant
        return CORPORATE_ROSTER, CORPORATE_SECRETS
    return MYSTERY_ROSTER, MYSTERY_SECRETS


async def _tenant(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with unscoped_session() as s:
        tid = await s.scalar(text("select id from tenant where slug=:s"), {"s": slug})
    if tid is None:
        tid, wid = await create_tenant("Secrets Showcase", slug)
    else:
        async with tenant_scope(tid) as s:
            wid = await s.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))
    # The gate is workspace-opt-in (it costs one extra model call per secret-holding
    # turn); the whole point of this seed is exercising it.
    async with tenant_scope(tid) as s:
        row = await s.get(Workspace, wid)
        row.settings = {**dict(row.settings), "secrets_gate": True}
    return tid, wid


async def _owner(tid: uuid.UUID, wid: uuid.UUID, slug: str) -> uuid.UUID:
    email = OWNER_EMAIL_FMT.format(slug=slug)
    async with tenant_scope(tid) as s:
        pid = await s.scalar(
            select(Identity.principal_id).where(
                Identity.provider == "local", Identity.external_id == email
            )
        )
        if pid is None:
            principal = Principal(tenant_id=tid, kind="human", display_name="Owner")
            s.add(principal)
            await s.flush()
            pid = principal.id
            s.add(Membership(tenant_id=tid, principal_id=pid, role="owner"))
            s.add(
                Identity(
                    tenant_id=tid,
                    principal_id=pid,
                    provider="local",
                    external_id=email,
                    password_hash=PasswordHasher().hash(VERIFY_PASSWORD),
                )
            )
        else:
            await s.execute(
                text(
                    "update identity set password_hash=:h where provider='local' and external_id=:e"
                ),
                {"h": PasswordHasher().hash(VERIFY_PASSWORD), "e": email},
            )
        has = await s.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == wid,
                WorkspaceMembership.principal_id == pid,
            )
        )
        if has is None:
            s.add(
                WorkspaceMembership(
                    tenant_id=tid, workspace_id=wid, principal_id=pid, role="overseer"
                )
            )
    return pid


async def _persona(tid, wid, key, name, ptype, persona_md) -> uuid.UUID:
    async with tenant_scope(tid) as s:
        agent_id = await s.scalar(
            select(Agent.id).where(Agent.name == "ss-model", Agent.archived_at.is_(None))
        )
    if agent_id is None:
        agent = await create_agent(
            tid, "ss-model", "ollama", MODEL, api_base=OLLAMA, encryptor=get_encryptor()
        )
        agent_id = agent.id
    async with tenant_scope(tid) as s:
        persona = await s.scalar(
            select(Persona).where(
                Persona.workspace_id == wid, Persona.key == key, Persona.archived_at.is_(None)
            )
        )
    if persona is None:
        persona = await create_persona(
            tid, wid, key, name, agent_id, persona_type=ptype, persona_md=persona_md
        )
    async with tenant_scope(tid) as s:
        has = await s.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == wid,
                WorkspaceMembership.principal_id == persona.principal_id,
            )
        )
        if has is None:
            role = "facilitator" if ptype == "supervisor" else "participant"
            s.add(
                WorkspaceMembership(
                    tenant_id=tid, workspace_id=wid, principal_id=persona.principal_id, role=role
                )
            )
    return persona.id


async def _secrets(tid, wid, gm_persona_id, gm_principal_id, secret_rows) -> int:
    async with tenant_scope(tid) as s:
        existing = await s.scalar(
            text("select count(*) from secret where workspace_id=:w and subject_id=:sid"),
            {"w": wid, "sid": gm_persona_id},
        )
    if existing:
        return int(existing)
    embedder = get_embedding_provider()
    made = 0
    for gist, plaintext, hint, directive in secret_rows:
        vec = (await embedder.embed(EmbedRequest(model=embedder.model_name, texts=[gist])))[0]
        async with tenant_scope(tid) as s:
            secret = SecretRow(
                tenant_id=tid,
                workspace_id=wid,
                subject_kind="agent",
                subject_id=gm_persona_id,
                content_ciphertext=get_encryptor().encrypt(plaintext),
                gist=gist,
                hint_text=hint,
                behavioral_directive=directive,
                scope_key="workspace_public",
            )
            s.add(secret)
            await s.flush()
            await s.execute(
                text("update secret set gist_embedding = CAST(:v AS vector) where id = :id"),
                {"v": "[" + ",".join(str(x) for x in vec) + "]", "id": secret.id},
            )
            s.add(
                SecretHolderRow(
                    tenant_id=tid,
                    secret_id=secret.id,
                    holder_principal_id=gm_principal_id,
                    holder_kind="author",
                )
            )
        made += 1
    return made


async def _definition(tid, wid, owner) -> uuid.UUID:
    for row in await list_definitions(tid):
        if row.key == "secrets_table":
            return row.id
    row = await create_definition(
        tid, "secrets_table", "Secrets Table", SHOWCASE_FLOW, workspace_id=wid, created_by=owner
    )
    return row.id


async def main(slug: str) -> None:
    tid, wid = await _tenant(slug)
    owner = await _owner(tid, wid, slug)
    roster, secret_rows = _variant(slug)
    ids: dict[str, uuid.UUID] = {}
    for key, name, ptype, md in roster:
        ids[key] = await _persona(tid, wid, key, name, ptype, md)
    async with tenant_scope(tid) as s:
        gm_principal = await s.scalar(
            select(Persona.principal_id).where(Persona.id == ids["ss-gm"])
        )
    n_secrets = await _secrets(tid, wid, ids["ss-gm"], gm_principal, secret_rows)
    definition = await _definition(tid, wid, owner)

    print(f"tenant={tid}")
    print(f"workspace={wid}")
    print(f"login={OWNER_EMAIL_FMT.format(slug=slug)} password={VERIFY_PASSWORD}")
    print(f"gm={ids['ss-gm']}")
    print(f"players={ids['ss-p1']},{ids['ss-p2']}")
    print(f"definition={definition}")
    print(f"secrets={n_secrets}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "secrets"))
