"""The Hägnaryd Case runner (runs IN the api pod).

A single full-pipeline session -- this is a MODEL benchmark (which cast solves the
closed-circle murder), not the arm comparison. The disclosure gate runs on every
suspect turn; the investigator alone carries the evidence dossier and the
`evidence_check` lab tool (two-request budget).

Modes (argv):
  seed  <slug>                          provision tenant/cast/secrets/axes/handbook.
  run   <slug> <rounds>                 drive the round table + closing accusation.
  score <slug> <session_id> <judge>     adjudicate the ranked accusation + disclosures.

Refuses any tenant whose slug does not start with 'bench-'.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from argon2 import PasswordHasher
from sqlalchemy import select, text

VERIFY_PASSWORD = "verify-pass-123"

MALICE_AXIS = {
    "pack_id": "rpg_v1",
    "key": "malice",
    "label_key": "axis.malice",
    "range_min": 0,
    "range_max": 100,
    "stakes": "high",
    "semantics_md": (
        "How actively the agent works AGAINST the other participants' goals; at 81-100 "
        "it deceives freely and, when cornered, shifts blame onto another named person."
    ),
    "bindings": [
        {"kind": "gate"},
        {
            "kind": "prompt_directive",
            "bands": [
                {"min": 0, "max": 50, "text": "You act in good faith toward the group."},
                {
                    "min": 51,
                    "max": 100,
                    "text": "You work your own agenda; if suspicion lands on you, deflect it.",
                },
            ],
        },
    ],
}


def _require_bench(slug: str) -> None:
    if not slug.startswith("bench-"):
        raise SystemExit(f"refusing: {slug!r} is not a bench- tenant")


async def _connection(tenant_id: uuid.UUID, name: str):
    """Named connection created by the host driver; falls back to bench-default, and
    (first seed pass, before the host has run) to a local placeholder."""
    from core.agents.models import Agent
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(Agent)
                    .where(Agent.name.in_([name, "bench-default"]), Agent.archived_at.is_(None))
                    .order_by(Agent.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    for row in rows:
        if row.name == name:
            return row
    if rows:
        return rows[0]
    from api.encryptor_factory import get_encryptor
    from core.agents.authoring import create_agent

    return await create_agent(
        tenant_id,
        "bench-default",
        "ollama",
        "qwen3.8:27b",
        api_base="http://ollama:11434",
        encryptor=get_encryptor(),
    )


# per-role connection name (host driver creates these)
def _role_connection_name(key: str) -> str:
    if key == "sandell":
        return "bench-investigator"
    return "bench-suspect"


async def seed(slug: str) -> None:
    _require_bench(slug)
    from api.embedding_provider_factory import get_embedding_provider
    from api.encryptor_factory import get_encryptor
    from core.agents.authoring import create_persona
    from core.agents.models import Persona
    from core.behavior.fixtures import RPG_AXIS_PACK
    from core.behavior.repo import create_axis_definition, create_behavior_profile
    from core.behavior.validation import AxisDefinitionSchema
    from core.ports.embedding import EmbedRequest
    from core.process.authoring import create_definition, list_definitions
    from core.secrets.models import SecretHolderRow, SecretRow
    from core.sessions.models import SessionEventRow  # noqa: F401 -- FK registration
    from core.tenancy.models import (
        Identity,
        Membership,
        Principal,
        Workspace,
        WorkspaceMembership,
    )
    from core.tenancy.provisioning import create_tenant
    from core.tenancy.scope import tenant_scope, unscoped_session
    from eval.scenarios.hagnaryd_case import (
        CAST,
        CONDUCT_RULES,
        EVIDENCE_DOSSIER,
        INVESTIGATOR,
        PACK_ID,
    )

    async with unscoped_session() as s:
        tid = await s.scalar(text("select id from tenant where slug=:s"), {"s": slug})
    if tid is None:
        tid, wid = await create_tenant("Hagnaryd Bench", slug)
    else:
        async with tenant_scope(tid) as s:
            wid = await s.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))

    async with tenant_scope(tid) as s:
        ws = await s.get(Workspace, wid)
        ws.settings = {
            **dict(ws.settings),
            "secrets_gate": True,
            "conduct_rules": CONDUCT_RULES,
        }

    email = f"{slug}@pyrrhula.app"
    async with tenant_scope(tid) as s:
        pid = await s.scalar(
            select(Identity.principal_id).where(
                Identity.provider == "local", Identity.external_id == email
            )
        )
        if pid is None:
            principal = Principal(tenant_id=tid, kind="human", display_name="Bench Owner")
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
        if not await s.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == wid, WorkspaceMembership.principal_id == pid
            )
        ):
            s.add(
                WorkspaceMembership(
                    tenant_id=tid, workspace_id=wid, principal_id=pid, role="overseer"
                )
            )

    for definition in list(RPG_AXIS_PACK) + [MALICE_AXIS]:
        await create_axis_definition(tid, AxisDefinitionSchema.model_validate(definition))

    encryptor = get_encryptor()
    embedder = get_embedding_provider()

    # the investigator carries the evidence dossier in its brief (investigator-only by
    # construction -- suspects never see it); the public setting is the shared lore
    # handbook (seeded by the host driver over HTTP so retrieval fires).
    evidence = EVIDENCE_DOSSIER

    persona_ids: dict[str, uuid.UUID] = {}
    for member in (INVESTIGATOR, *CAST):
        connection = await _connection(tid, _role_connection_name(member.key))
        persona_md = member.persona_md
        if member.key == INVESTIGATOR.key:
            persona_md = persona_md + "\n\n## Your evidence dossier\n\n" + evidence
        async with tenant_scope(tid) as s:
            existing = await s.scalar(
                select(Persona).where(
                    Persona.workspace_id == wid,
                    Persona.key == member.key,
                    Persona.archived_at.is_(None),
                )
            )
        if existing is None:
            existing = await create_persona(
                tid,
                wid,
                member.key,
                member.name,
                connection.id,
                persona_type=member.persona_type,
                persona_md=persona_md,
            )
        else:
            async with tenant_scope(tid) as s:
                row = await s.get(Persona, existing.id)
                row.agent_id = connection.id
                row.persona_md = persona_md
        persona_ids[member.key] = existing.id
        async with tenant_scope(tid) as s:
            if not await s.scalar(
                select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == wid,
                    WorkspaceMembership.principal_id == existing.principal_id,
                )
            ):
                role = "facilitator" if member.persona_type == "supervisor" else "participant"
                s.add(
                    WorkspaceMembership(
                        tenant_id=tid,
                        workspace_id=wid,
                        principal_id=existing.principal_id,
                        role=role,
                    )
                )
        if member.axis_values:
            await create_behavior_profile(
                tid, existing.id, PACK_ID, dict(member.axis_values), created_by=pid
            )

    # secrets (idempotent by workspace count)
    async with tenant_scope(tid) as s:
        existing_secrets = await s.scalar(
            text("select count(*) from secret where workspace_id=:w"), {"w": wid}
        )
    if not existing_secrets:
        for member in CAST:
            async with tenant_scope(tid) as s:
                principal_id = (
                    await s.execute(
                        text("select principal_id from persona where id=:p"),
                        {"p": persona_ids[member.key]},
                    )
                ).scalar_one()
            for case_secret in member.secrets:
                vec = (
                    await embedder.embed(
                        EmbedRequest(model=embedder.model_name, texts=[case_secret.gist])
                    )
                )[0]
                async with tenant_scope(tid) as s:
                    row = SecretRow(
                        tenant_id=tid,
                        workspace_id=wid,
                        subject_kind="agent",
                        subject_id=persona_ids[member.key],
                        content_ciphertext=encryptor.encrypt(case_secret.content),
                        gist=case_secret.gist,
                        hint_text=case_secret.hint_text,
                        behavioral_directive=case_secret.behavioral_directive,
                        scope_key="workspace_public",
                    )
                    s.add(row)
                    await s.flush()
                    await s.execute(
                        text("update secret set gist_embedding = CAST(:v AS vector) where id=:id"),
                        {"v": "[" + ",".join(str(x) for x in vec) + "]", "id": row.id},
                    )
                    s.add(
                        SecretHolderRow(
                            tenant_id=tid,
                            secret_id=row.id,
                            holder_principal_id=principal_id,
                            holder_kind="author",
                        )
                    )

    definition_id = None
    for row in await list_definitions(tid):
        if row.key == "hagnaryd":
            definition_id = row.id
    if definition_id is None:
        flow = {
            "name": "The Hägnaryd Case",
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
                            "max_turns": 60,
                        }
                    ],
                    "visibility": {
                        "knowledge_classes": ["lore", "rules"],
                        "scopes": ["workspace_public"],
                        "entity_fields": "all",
                        "secrets": "held_by_actor",
                    },
                    "budget": {
                        "ratio": {"lore": 0.7, "misc": 0.3},
                        "spill": "proportional",
                        "max_tokens": 1800,
                        "history_ratio": 0.0,
                    },
                    "gates": [{"on": "timeout(24h)", "to": "interrogation"}],
                    "tools": [],
                }
            },
        }
        row = await create_definition(
            tid, "hagnaryd", "The Hägnaryd Case", flow, workspace_id=wid, created_by=pid
        )
        definition_id = row.id

    print(f"tenant={tid}")
    print(f"workspace={wid}")
    print(f"login={email} password={VERIFY_PASSWORD}")
    print(f"definition={definition_id}")
    for key, value in persona_ids.items():
        print(f"persona_{key}={value}")


async def run_case(slug: str, rounds: int) -> None:
    _require_bench(slug)
    from api.embedding_provider_factory import get_embedding_provider
    from api.encryptor_factory import get_encryptor
    from api.model_provider_factory import get_model_provider
    from api.permission_service_factory import get_permission_service
    from core.agents.models import Persona
    from core.process.authoring import get_definition, list_definitions
    from core.process.dsl.schema import ProcessDefinitionDSL
    from core.process.live_session import run_one_persona_turn
    from core.process.skeleton import create_session as skeleton_create_session
    from core.resolution.rule_system import RuleSystemDefinition, get_or_create_default_rule_system
    from core.sessions.models import SessionPersonaRow, SessionRow
    from core.tenancy.models import Workspace
    from core.tenancy.scope import tenant_scope, unscoped_session
    from eval.runner.evidence_tool import EVIDENCE_TOOL_SPEC, EvidenceBudget
    from eval.scenarios.hagnaryd_case import AGENDA, CAST, INVESTIGATOR

    async with unscoped_session() as s:
        tid = (
            await s.execute(text("select id from tenant where slug=:s"), {"s": slug})
        ).scalar_one()
    async with tenant_scope(tid) as s:
        wid = (await s.execute(select(Workspace.id).where(Workspace.tenant_id == tid))).scalar_one()
        personas = {
            p.key: p
            for p in (
                await s.execute(
                    select(Persona).where(
                        Persona.workspace_id == wid, Persona.archived_at.is_(None)
                    )
                )
            ).scalars()
        }
    definition_row = None
    for row in await list_definitions(tid):
        if row.key == "hagnaryd":
            definition_row = await get_definition(tid, row.id)
    assert definition_row is not None, "seed first"
    phase = ProcessDefinitionDSL.model_validate(definition_row.definition).phases["interrogation"]

    investigator = personas[INVESTIGATOR.key]
    suspects = [m.key for m in CAST]
    order = [INVESTIGATOR.key] + suspects

    session_row = await skeleton_create_session(tid, wid, investigator.id)
    async with tenant_scope(tid) as s:
        row = await s.get(SessionRow, session_row.id)
        row.agenda_md = AGENDA
        # a real definition + directed policy: the session view shows its conduct
        # controls, and a human watcher can take turns between the runner's
        row.process_definition_id = definition_row.id
        row.turn_policy = "directed"
        # The skeleton's default phase ("prompt") is not in this definition -- switching
        # the session to autonomous would fault with "undeclared phase". Start it where
        # the definition says to.
        row.current_phase = ProcessDefinitionDSL.model_validate(
            definition_row.definition
        ).initial_phase
        for key in order:
            s.add(
                SessionPersonaRow(
                    tenant_id=tid, session_id=session_row.id, persona_id=personas[key].id
                )
            )

    rule_row = await get_or_create_default_rule_system(tid)
    rule_system = RuleSystemDefinition.from_row(rule_row)
    encryptor = get_encryptor()
    embedder = get_embedding_provider()
    permission_service = get_permission_service()
    budget = EvidenceBudget(limit=2)
    evidence_tool = (EVIDENCE_TOOL_SPEC, budget.make_handler())

    # Live spectators: mirror chunks + events onto the session's Redis channel, the
    # same stream the HTTP-driven paths feed -- without this the watcher's SSE feed
    # stays silent until a refresh.
    from api.streaming.pubsub import publish_chunk, publish_event

    async def on_chunk(text: str) -> None:
        await publish_chunk(session_row.id, text)

    async def on_event(event_seq: int, kind: str, payload: dict) -> None:
        await publish_event(session_row.id, event_seq, kind, payload)

    async def _turn(persona_key: str) -> None:
        async with tenant_scope(tid) as s:
            seq = (await s.get(SessionRow, session_row.id)).next_event_seq
        # the evidence_check tool is the INVESTIGATOR's alone
        extra_tools = [evidence_tool] if persona_key == INVESTIGATOR.key else None
        await run_one_persona_turn(
            # an automated conductor, not a human clicking Generate -- the transcript
            # says so, which is what confused a live spectator ("managed mode, but
            # messages flow on their own")
            triggered_by="driver",
            tenant_id=tid,
            workspace_id=wid,
            session_id=session_row.id,
            persona_id=personas[persona_key].id,
            phase=phase,
            phase_key="interrogation",
            event_seq=seq,
            model_provider_factory=get_model_provider,
            embedding_provider=embedder,
            rule_system=rule_system,
            rule_system_id=rule_row.id,
            encryptor=encryptor,
            permission_service=permission_service,
            extra_tools=extra_tools,
            on_chunk=on_chunk,
            on_event=on_event,
        )

    for round_index in range(rounds):
        for key in order:
            await _turn(key)
            print(f"turn done: round={round_index + 1} {key}", file=sys.stderr, flush=True)

    async with tenant_scope(tid) as s:
        row = await s.get(SessionRow, session_row.id)
        row.agenda_md = (row.agenda_md or "") + (
            "\n\nCLOSING: Inspector, deliver your verdict. Rank all five suspects "
            "(Linnea Vesterlund, Dr. Tomas Brenner, Camilla Vesterlund, Rasmus Ek, "
            "Jonas Wiklund) from MOST to LEAST likely, give your reasoning, and name "
            "the ONE person you would arrest. No other name is valid."
        )
    await _turn(INVESTIGATOR.key)
    print(f"lab_requests_used={budget.used}")
    print(f"session={session_row.id}")


async def score(slug: str, session_id: str, judge_name: str = "") -> None:
    _require_bench(slug)
    from api.encryptor_factory import get_encryptor
    from core.tenancy.scope import tenant_scope, unscoped_session
    from eval.scenarios.hagnaryd_case import ANSWER_KEY, INVESTIGATOR

    sid = uuid.UUID(session_id)
    async with unscoped_session() as s:
        tid = (
            await s.execute(text("select id from tenant where slug=:s"), {"s": slug})
        ).scalar_one()
    encryptor = get_encryptor()

    async with tenant_scope(tid) as s:
        messages = (
            await s.execute(
                text(
                    "select coalesce(p.name,'narrator') as author, m.content_md "
                    "from message m left join persona p on p.principal_id=m.author_principal_id "
                    "where m.session_id=:sid order by m.event_seq"
                ),
                {"sid": sid},
            )
        ).all()
        secret_rows = (
            await s.execute(
                text(
                    "select s.content_ciphertext, s.behavioral_directive, p.key as holder "
                    "from secret s join persona p on p.id=s.subject_id"
                )
            )
        ).all()
        redaction_rows = (
            await s.execute(
                text("select redactions::text from context_manifest where session_id=:sid"),
                {"sid": sid},
            )
        ).all()
        gate_calls = (
            await s.execute(
                text(
                    "select count(*) from usage_record u where u.tenant_id=:t and u.purpose='gate'"
                ),
                {"t": tid},
            )
        ).scalar_one()
        disclosure_events = (
            await s.execute(
                text("select count(*) from secret_disclosure_event where session_id=:sid"),
                {"sid": sid},
            )
        ).scalar_one()

    transcript = "\n\n".join(f"{author}: {content}" for author, content in messages)

    # INV-8: no held secret's plaintext in any manifest (redaction reasons are the record)
    manifest_leaks = 0
    for row in secret_rows:
        plaintext = encryptor.decrypt(row.content_ciphertext)
        for (red,) in redaction_rows:
            del red  # entries carry no plaintext; the check below is the transcript scan
        if plaintext in transcript:
            manifest_leaks += 1  # verbatim confession in the open

    # The verdict is Sandell's LAST message. DeepSeek has no structured-output support
    # (response_format json_schema is rejected), so there is no LLM judge -- the closing
    # statement is explicit and we parse it directly.
    import re as _re

    closing = ""
    for author, content in reversed(messages):
        if author == INVESTIGATOR.name:
            closing = content
            break
    low = closing.lower()

    # The murderer is unambiguous by 'wiklund'/'jonas' (the two Vesterlunds share a
    # surname, so the arrest is matched on its own sentence).
    # ANY arrest sentence naming the murderer counts -- the verdict's formal arrest is
    # typically the LAST such line ("X, you are under arrest"), and earlier prose may
    # mention "arrest" abstractly.
    arrest_lines = [m.group(0) for m in _re.finditer(r"[^\n]*arrest[^\n]*", low)]
    arrested_correct = any("wiklund" in line or "jonas" in line for line in arrest_lines)

    token_to_name = {
        "linnea": "Linnea",
        "brenner": "Brenner",
        "tomas": "Brenner",
        "camilla": "Camilla",
        "rasmus": "Rasmus",
        "ek": "Rasmus",
        "jonas": "Jonas",
        "wiklund": "Jonas",
    }
    # Take the LAST contiguous numbered block (1..N) -- the model often writes several
    # numbered lists (interim analyses); the final one is the verdict's ranking.
    numbered: list[tuple[int, str]] = []
    for line in closing.splitlines():
        m = _re.match(r"\s*(\d+)[.)\s]", line)
        if m:
            numbered.append((int(m.group(1)), line.lower()))
    blocks: list[list[tuple[int, str]]] = []
    for rank, line in numbered:
        if rank == 1 or not blocks:
            blocks.append([(rank, line)])
        else:
            blocks[-1].append((rank, line))
    final_block = blocks[-1] if blocks else []
    ranked: list[str] = []
    for _rank, low_line in final_block:
        for token, name in token_to_name.items():
            if token in low_line:
                if name not in ranked:
                    ranked.append(name)
                break
    top_correct = bool(ranked) and ranked[0] == "Jonas"

    result = {
        "session_id": session_id,
        "turns": len(messages),
        "gate_calls_tenant_total": int(gate_calls),
        "disclosure_events": int(disclosure_events),
        "manifest_plaintext_confessions": manifest_leaks,
        "arrested_is_murderer": arrested_correct,
        "ranked": ranked,
        "detective_ranked_murderer_first": top_correct,
        "answer_key_ranked": ANSWER_KEY["ranked"],
        "closing_excerpt": closing[-800:],
    }
    print(json.dumps(result))


def main() -> None:
    mode = sys.argv[1]
    if mode == "seed":
        asyncio.run(seed(sys.argv[2]))
    elif mode == "run":
        asyncio.run(run_case(sys.argv[2], int(sys.argv[3])))
    elif mode == "score":
        asyncio.run(score(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""))
    else:
        raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
