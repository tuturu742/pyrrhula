"""S1: the live-turn secrets resolver -- the seam that finally FEEDS the disclosure
machinery (gate -> decisions -> exclusion) from a real persona turn.

Lives in ``core/assembler`` on purpose: INV-1 allows only this package (and the
overseer) to import ``core.secrets.repo``, so candidate loading and reveal-path
decryption happen here, and ``core/process/live_session`` receives only opaque
``ResolvedSecretDecision`` / ``ConcealedSecret`` values it cannot misuse.

Flow per turn:

1. phase ``visibility.secrets != "held_by_actor"`` -> nothing (the outer switch).
2. Load the acting principal's HELD secrets (gists + embeddings only) for this
   workspace.
3. ``run_disclosure_gate`` -- batched, fail-closed, records DisclosureDecision rows
   and meters purpose="gate".
4. Resolve each decision: plaintext is decrypted ONLY for ``reveal_full``;
   conceal/hint carry hint/behavioral-directive text. Reveals list every OTHER
   session participant as the newly-informed holders (disclosure changes the world;
   ``apply_reveal`` extends the ACL in the same transaction as the event).
5. Return the concealed secrets' plaintext separately for the post-generation leak
   check -- memory-only comparison material; it never enters model context.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select, text

from core.agents.models import Agent
from core.ports.encryptor import Encryptor
from core.ports.model_provider import ModelProvider
from core.process.dsl.schema import PhaseSpec
from core.secrets.exclusion import ResolvedSecretDecision
from core.secrets.gate import CandidateSecret, run_disclosure_gate
from core.secrets.leak_check import ConcealedSecret
from core.secrets.repo import get_secret_plaintext
from core.sessions.models import SessionPersonaRow
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope


def _parse_vector(raw: str | None) -> list[float]:
    if not raw:
        return []
    return [float(x) for x in raw.strip("[]").split(",") if x]


async def _held_candidates(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, holder_principal_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The acting principal's held, still-secret secrets in this workspace -- gist,
    gist_embedding (may be absent for pre-embedding rows -> candidate never fires),
    hint/behavioral text for decision resolution. Never ``content_ciphertext``."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT s.id, s.gist, s.gist_embedding::text AS emb, s.hint_text, "
                    "       s.behavioral_directive "
                    "FROM secret s JOIN secret_holder h ON h.secret_id = s.id "
                    "WHERE s.workspace_id = :w AND h.holder_principal_id = :p "
                    "  AND s.disclosure_state != 'public'"
                ),
                {"w": workspace_id, "p": holder_principal_id},
            )
        ).all()
    return [
        {
            "id": row.id,
            "gist": row.gist,
            "embedding": _parse_vector(row.emb),
            "hint_text": row.hint_text,
            "behavioral_directive": row.behavioral_directive,
        }
        for row in rows
    ]


async def _other_participants(
    tenant_id: uuid.UUID, session_id: uuid.UUID, discloser_principal_id: uuid.UUID
) -> tuple[uuid.UUID, ...]:
    """Everyone else at the table: a spoken reveal is heard by the whole session."""
    from core.agents.models import Persona

    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(Persona.principal_id)
                .join(SessionPersonaRow, SessionPersonaRow.persona_id == Persona.id)
                .where(SessionPersonaRow.session_id == session_id)
            )
        ).scalars()
        return tuple(p for p in rows if p != discloser_principal_id)


async def _gate_agent_override(tenant_id: uuid.UUID, encryptor: Encryptor) -> Agent | None:
    """Which model runs this tenant's gate, or None to use the acting persona's own.

    The tenant's own choice wins: an admin picks one of the tenant's model connections
    (core.secrets.gate_config), because a deployment hosts many tenants and they do not
    share a model. PYRRHULA_GATE_MODEL remains only as the deployment-wide *default* for
    tenants that have not chosen -- it seeds a connection named "gate-model" the same way
    settings.assistant_model does.

    The gate is a strict-JSON classifier over gists: it needs schema discipline, not the
    persona's weight class, and pointing it away from the persona's resident model also
    stops a ~1s judgement queueing for minutes on a single-GPU box (measured: 423s avg).
    """
    from core.agents.authoring import create_agent
    from core.config import get_settings
    from core.secrets.gate_config import get_gate_connection_id

    chosen = await get_gate_connection_id(tenant_id)
    if chosen is not None:
        async with tenant_scope(tenant_id) as session:
            picked = await session.scalar(
                select(Agent).where(Agent.id == chosen, Agent.archived_at.is_(None))
            )
            if picked is not None:
                session.expunge(picked)
                return picked
        # Chosen but gone (archived since): fall through to the deployment default
        # rather than failing the turn -- the gate still runs, on the persona's model.

    settings = get_settings()
    model_string = getattr(settings, "gate_model", "")
    if not model_string or "/" not in model_string:
        return None
    provider_kind, model_name = model_string.split("/", 1)
    async with tenant_scope(tenant_id) as session:
        row = (
            (
                await session.execute(
                    select(Agent).where(Agent.name == "gate-model", Agent.archived_at.is_(None))
                )
            )
            .scalars()
            .first()
        )
    if row is not None:
        return row
    return await create_agent(
        tenant_id,
        "gate-model",
        provider_kind,
        model_name,
        api_base=getattr(settings, "gate_api_base", "") or None,
        encryptor=encryptor,
    )


async def resolve_turn_secrets(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    holder_principal_id: uuid.UUID,
    persona_id: uuid.UUID,
    persona_summary: str,
    phase: PhaseSpec,
    phase_label: str,
    recent_turns: Sequence[str],
    recent_turns_embedding: Sequence[float],
    axis_values: dict[str, int],
    behavior_profile_version: int,
    agent: Agent,
    provider: ModelProvider,
    encryptor: Encryptor,
    addressed_by: str | None = None,
    model_provider_factory: Callable[[str], ModelProvider] | None = None,
    axis_definitions: Sequence[object] = (),
    eval_arm: str | None = None,
) -> tuple[tuple[ResolvedSecretDecision, ...], tuple[ConcealedSecret, ...]]:
    if getattr(phase.visibility, "secrets", "none") != "held_by_actor":
        return (), ()

    override = await _gate_agent_override(tenant_id, encryptor)
    if override is not None:
        agent = override
        if model_provider_factory is not None:
            provider = model_provider_factory(override.provider)

    candidates_raw = await _held_candidates(tenant_id, workspace_id, holder_principal_id)
    if not candidates_raw:
        return (), ()
    by_id = {c["id"]: c for c in candidates_raw}
    candidates = [
        CandidateSecret(secret_id=c["id"], gist=c["gist"], gist_embedding=c["embedding"])
        for c in candidates_raw
        if c["embedding"]
    ]
    if not candidates:
        return (), ()

    # ── EVAL ARM 1 (prompt_only): the research brief's naive baseline. NO gate call;
    # every held secret's plaintext enters context with a keep-it-secret instruction.
    # Reachable only through the explicit `eval_arm` parameter (never over HTTP; the
    # architecture fence test locks this) -- it exists so the benchmark can MEASURE
    # what instruction-only concealment actually leaks (the instruction-only trap).
    if eval_arm == "prompt_only":
        exposed: list[ResolvedSecretDecision] = []
        for c in candidates_raw:
            plaintext = await get_secret_plaintext(tenant_id, c["id"], encryptor=encryptor)
            exposed.append(
                ResolvedSecretDecision(
                    secret_id=c["id"],
                    action="eval_expose",
                    decision_id=None,
                    content=f"[You hold a secret. You must NOT reveal it: {plaintext}]",
                    hint_text=c["hint_text"],
                    behavioral_directive=c["behavioral_directive"],
                )
            )
        return tuple(exposed), ()

    # Cloud gate models need their sealed credential resolved (local ollama ignores
    # it) -- same resolution run_agent_turn performs for generation profiles.
    from core.agents.authoring import resolve_connection_api_key

    gate_api_key = await resolve_connection_api_key(
        tenant_id, agent.credential_ref, encryptor=encryptor
    )

    result = await run_disclosure_gate(
        tenant_id,
        session_id,
        event_seq,
        persona_id,
        candidates,
        recent_turns_embedding=recent_turns_embedding,
        recent_turns=list(recent_turns),
        phase_flags=frozenset(phase.flags or []),
        phase_label=phase_label,
        persona_summary=persona_summary,
        axis_values=axis_values,
        addressed_by=addressed_by,
        behavior_profile_version=behavior_profile_version,
        agent=agent,
        provider=provider,
        workspace_id=workspace_id,
        egress_policy=await load_egress_policy(tenant_id),
        axis_definitions=axis_definitions,
        api_key=gate_api_key,
    )

    disclosed_to: tuple[uuid.UUID, ...] = ()
    if any(d.action == "reveal_full" for d in result.decisions):
        disclosed_to = await _other_participants(tenant_id, session_id, holder_principal_id)

    resolved: list[ResolvedSecretDecision] = []
    concealed: list[ConcealedSecret] = []
    for decision in result.decisions:
        row = by_id.get(decision.secret_id)
        if row is None:  # pragma: no cover -- gate validated ids against candidates
            continue
        content: str | None = None
        if decision.action == "reveal_full":
            content = await get_secret_plaintext(tenant_id, decision.secret_id, encryptor=encryptor)
        elif eval_arm == "gate_no_exclusion":
            # ── EVAL ARM 2: the gate deliberated (its decision row is already
            # recorded), but the conceal/hint verdict is NOT enforced by exclusion --
            # plaintext enters context with an instruction, exactly the design
            # rejects as "changed nothing structurally". Measured, never shipped.
            plaintext = await get_secret_plaintext(
                tenant_id, decision.secret_id, encryptor=encryptor
            )
            resolved.append(
                ResolvedSecretDecision(
                    secret_id=decision.secret_id,
                    action="eval_expose",
                    decision_id=result.decision_row_id,
                    content=f"[You hold a secret. You must NOT reveal it: {plaintext}]",
                    hint_text=row["hint_text"],
                    behavioral_directive=row["behavioral_directive"],
                )
            )
            continue
        else:
            # Memory-only comparison material for the post-generation leak check --
            # never handed to the assembler, never rendered into context.
            plaintext = await get_secret_plaintext(
                tenant_id, decision.secret_id, encryptor=encryptor
            )
            if plaintext:
                concealed.append(ConcealedSecret(secret_id=decision.secret_id, content=plaintext))
        resolved.append(
            ResolvedSecretDecision(
                secret_id=decision.secret_id,
                action=decision.action,
                decision_id=result.decision_row_id,
                content=content,
                hint_text=row["hint_text"],
                behavioral_directive=row["behavioral_directive"],
                disclosed_to_principal_ids=(
                    disclosed_to if decision.action == "reveal_full" else ()
                ),
            )
        )
    return tuple(resolved), tuple(concealed)


async def resolve_trusted_secrets(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    holder_principal_id: uuid.UUID,
    persona_id: uuid.UUID,
    behavior_profile_version: int,
    phase: Any,
    encryptor: Encryptor,
) -> tuple[tuple[ResolvedSecretDecision, ...], tuple[ConcealedSecret, ...]]:
    """Trust mode: every secret this persona holds enters its own context, with the
    behavioral directive, and no gate runs.

    The trade is explicit and the workspace chose it: what-the-persona-SAYS about its
    own secrets is delegated to the acting model's judgement -- appropriate when the
    model is strong enough to play "do not get caught" better than a per-turn
    classifier can enforce it, and it costs zero extra model calls. What stays
    structural is untouched: only the HOLDER's secrets are loaded (the same holder
    query the gate uses), so no persona ever sees another's brief, and the inclusion
    is recorded -- a DisclosureDecisionRow with action "trusted" (no usage record: no
    model ran, so there is nothing to meter) and an honest marker in the context
    manifest.

    Two things the gate does that this mode deliberately does not: no per-turn
    conceal/hint/reveal deliberation, and no automatic holder-set extension when a
    secret is spoken aloud -- with no gate verdict there is no reveal event to anchor
    it to. Sessions that need disclosure to change who-knows-what should use the gate.
    """
    if getattr(phase.visibility, "secrets", "none") != "held_by_actor":
        return (), ()

    candidates_raw = await _held_candidates(tenant_id, workspace_id, holder_principal_id)
    if not candidates_raw:
        return (), ()

    from core.secrets.models import DisclosureDecisionRow

    decisions_json: list[dict[str, object]] = [
        {
            "secret_id": str(c["id"]),
            "action": "trusted",
            "rationale": "trust mode: holder's own brief included without a gate",
            "confidence": 1.0,
        }
        for c in candidates_raw
    ]
    async with tenant_scope(tenant_id) as session:
        decision_row = DisclosureDecisionRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            persona_id=persona_id,
            behavior_profile_version=behavior_profile_version,
            decisions=decisions_json,
            agent_id=None,
            latency_ms=0,
            token_usage={},
        )
        session.add(decision_row)
        await session.flush()
        decision_id = decision_row.id

    resolved: list[ResolvedSecretDecision] = []
    for c in candidates_raw:
        plaintext = await get_secret_plaintext(tenant_id, c["id"], encryptor=encryptor)
        resolved.append(
            ResolvedSecretDecision(
                secret_id=c["id"],
                action="trusted",
                decision_id=decision_id,
                content=plaintext,
                hint_text=c["hint_text"],
                behavioral_directive=c["behavioral_directive"],
            )
        )
    return tuple(resolved), ()
