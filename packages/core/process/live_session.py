"""B1.8: composition-root glue wiring the real interpreter (B1.2), scheduler (B1.3),
checkpoints (B1.4), awaits (B1.6), budget-aware context assembler (C1.2), and
tool-calling agent runtime (B1.7) together for a process-definition-backed session.
Every individual piece was already complete and tested before this module existed; this
is the first real caller of all of them together, matching this project's own
established "build the seam, wire it in once the real pieces exist" discipline.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from core.agents.authoring import HISTORY_CHAR_BUDGET_KEY, merged_persona_params
from core.agents.models import Agent, Persona
from core.agents.runtime import AllRetriesExhaustedError, ToolLoopExceededError, run_agent_turn
from core.agents.scheduling import make_persona_candidate_resolver
from core.agents.tools import ToolHandler, ToolRegistry
from core.assembler.context_assembler import assemble
from core.assembler.manifest import write_context_manifest
from core.behavior.directives import render_directives_for_profile
from core.behavior.repo import get_current_behavior_profile, list_axis_definitions
from core.entities.storage import EntityRow
from core.ports.embedding import EmbeddingProvider, EmbedRequest
from core.ports.encryptor import Encryptor
from core.ports.mcp import McpTransport
from core.ports.model_provider import GenerationRequest, ModelProvider, ToolSpec
from core.ports.moderation import ModerationProvider
from core.ports.permission import PermissionService
from core.ports.reranker import Reranker
from core.process.awaits import make_await_hook
from core.process.checkpoints import make_checkpoint_hook
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    AdvanceResult,
    HumanTurnPendingError,
    InterpreterContext,
    InterpreterFaultError,
    NextActorFn,
    OnEvent,
    advance_session,
)
from core.process.scheduler import make_default_candidate_resolver, make_scheduler
from core.process.session_entity_tools import (
    make_entity_create_handler,
    make_resolve_apply_handler,
)
from core.process.session_web_tools import (
    make_web_search_handler,
    workspace_has_web_search,
)
from core.resolution.registry import RANDOMIZER_DEFINITION, ensure_tool_definition
from core.resolution.rule_system import RuleSystemDefinition, get_or_create_default_rule_system
from core.resolution.service import ActorFieldsResolver, make_randomizer_handler
from core.sessions.lifecycle import resolve_author_name
from core.sessions.models import MessageRow, SessionPersonaRow, SessionRow
from core.tenancy.models import Principal, Workspace
from core.tenancy.scope import tenant_scope
from core.usage_limits import UsageLimitExceededError

ModelProviderFactory = Callable[[str], ModelProvider]
OnChunk = Callable[[str], Awaitable[None]]


# What a character is, when nothing says otherwise. A roll still has to resolve for an
# actor with no entity behind it -- an NPC the facilitator invented mid-scene, a session
# whose workspace never defined a sheet -- so this is the floor, not the answer.
_UNSHEETED_ACTOR_FIELDS: dict[str, object] = {"dexterity": 14, "strength": 14}


def _make_actor_fields_resolver(tenant_id: uuid.UUID) -> ActorFieldsResolver:
    """Read an actor's stats off its own entity row.

    This used to be a fixed ``{dexterity: 14, strength: 14}`` stub on the grounds that no
    entity system existed yet. One has existed since F3.6, and the stub outliving it meant
    every actor in every session rolled with identical stats: a randomizer that
    validates ``1d20+STR`` against a constant is theatre, and the sheet the player was
    handed was fiction.

    Entity ``data`` is read inside ``tenant_scope``, so an actor id from another tenant
    resolves to nothing rather than to someone else's character."""

    async def resolve(actor_entity_id: uuid.UUID | None) -> dict[str, object]:
        if actor_entity_id is None:
            return dict(_UNSHEETED_ACTOR_FIELDS)
        async with tenant_scope(tenant_id) as session:
            entity = await session.get(EntityRow, actor_entity_id)
            if entity is None or entity.tenant_id != tenant_id:
                return dict(_UNSHEETED_ACTOR_FIELDS)
            fields = dict(entity.data or {})
        # A sheet that omits an ability still has to answer for it -- fall back per key
        # rather than discarding the whole sheet because one stat is missing.
        for key, value in _UNSHEETED_ACTOR_FIELDS.items():
            fields.setdefault(key, value)
        return fields

    return resolve


async def _load_conversation(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer_principal_id: uuid.UUID | None = None,
    history_char_budget: int | None = None,
) -> list[dict[str, str]]:
    """The transcript as chat messages. With a ``viewer_principal_id``, roles are
    mapped from that actor's perspective: its own past turns keep their stored role,
    every other author's become ``user`` turns prefixed with the author's display
    name. Chat-completions backends assume a two-role dialogue (and some -- ollama's
    chat API among them -- reject a list with no ``user`` entry at all), so a
    multi-actor transcript replayed as all-``assistant`` both crashes those backends
    and misattributes everyone's words to the current speaker."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(MessageRow, Persona.name)
                .join(
                    Persona,
                    Persona.principal_id == MessageRow.author_principal_id,
                    isouter=True,
                )
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.event_seq)
            )
        ).all()
        out: list[dict[str, str]] = []
        for m, author_name in rows:
            if viewer_principal_id is None or m.author_principal_id == viewer_principal_id:
                out.append({"role": m.role, "content": m.content_md})
            else:
                content = f"{author_name}: {m.content_md}" if author_name else m.content_md
                out.append({"role": "user", "content": content})
        return _tail_trim(out, history_char_budget)


def _replayed_from_seq(conversation: list[dict[str, str]], event_seq: int) -> int:
    """The first event_seq still present verbatim in the replayed tail. Everything
    BEFORE it is what a history summary must cover -- the tail is trimmed by character
    budget (_tail_trim), so this is derived from how many messages survived, not from a
    fixed window. 0 means nothing was dropped: no summary needed."""
    dropped = max(0, event_seq - len(conversation))
    return dropped


# The transcript budget when nothing more specific is configured. It is a property of
# the model, not the deployment -- a local 8B on a laptop and a hosted frontier model do
# not want the same number -- so a connection or persona sets `history_char_budget` in
# its params and this is only the floor beneath them.
_DEFAULT_HISTORY_CHAR_BUDGET = 24000


def _tail_trim(
    conversation: list[dict[str, str]], budget: int | None = None
) -> list[dict[str, str]]:
    """Keep the newest messages within a character budget (always at least the two
    newest). Local models degrade into token noise well before their nominal context
    window on modest hardware, and the assembled manifest already carries the durable
    knowledge -- replaying the whole transcript verbatim is the part that grows without
    bound."""
    budget = int(budget or _DEFAULT_HISTORY_CHAR_BUDGET)
    kept: list[dict[str, str]] = []
    used = 0
    for message in reversed(conversation):
        if kept and len(kept) >= 2 and used + len(message["content"]) > budget:
            break
        kept.append(message)
        used += len(message["content"])
    kept.reverse()
    return kept


def _phase_remote_allowlist(phase: Any) -> list[str] | None:
    """The acting phase's remote-tool allowlist, or None for the legacy allow-all.

    Remote MCP tools used to reach every persona in every phase -- workspace
    registration was the only gate. For a cast with asymmetric knowledge that is wrong
    by construction: a murder-mystery suspect could call the investigator's forensic
    oracle and read the referee's answers. The phase declares who gets what, exactly as
    it already does for visibility.
    """
    allowed = getattr(phase, "remote_tools", None)
    if allowed is None:
        return None
    return [str(t) for t in allowed]


async def run_one_persona_turn(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    persona_id: uuid.UUID,
    phase: Any,
    phase_key: str,
    event_seq: int,
    model_provider_factory: ModelProviderFactory,
    embedding_provider: EmbeddingProvider,
    reranker: Reranker | None = None,
    rule_system: RuleSystemDefinition,
    rule_system_id: uuid.UUID,
    encryptor: Encryptor | None = None,
    on_chunk: OnChunk | None = None,
    permission_service: PermissionService | None = None,
    mcp_transport: McpTransport | None = None,
    moderation_provider: ModerationProvider | None = None,
    eval_arm: str | None = None,
    extra_tools: Sequence[tuple[ToolSpec, ToolHandler]] | None = None,
    on_event: OnEvent | None = None,
    # provenance for the transcript: scheduler | conducted | override | driver
    triggered_by: str = "unknown",
) -> ActorTurnResult:
    """One model-generated persona turn: context assembly (C1.2) + manifest write (C1.3) +
    tool-loop generation (B1.7), committed by ``run_agent_turn`` at the given ``event_seq``.

    ``eval_arm`` is the benchmark-only switch (plan §8.6 arms): no HTTP surface passes
    it, only the eval runner's direct core call -- enforced by
    ``tests/architecture/test_eval_arm_fence.py``. Arms 1/2 deliberately weaken
    exclusion to measure the designs §8.4 rejects; the leak check is skipped for them
    (the runner measures leaks itself -- regenerating away the evidence would defeat
    the measurement).

    Factored out of the scheduler-driven ``execute_turn`` so the *directed* path (#7: a human
    overseer triggers a specific persona's turn in managed mode) runs byte-for-byte the same
    context/leak/usage/idempotency handling as an autonomous turn -- the only difference is
    who chose the persona (the scheduler vs. the overseer), never how the turn is built."""
    async with tenant_scope(tenant_id) as session:
        persona = await session.get(Persona, persona_id)
        if persona is None or persona.tenant_id != tenant_id:
            raise InterpreterFaultError(
                f"persona {persona_id} is not a known persona in this tenant"
            )
        persona_type = persona.persona_type
        persona_name = persona.name
        persona_web_search = persona.web_search
        principal_id = persona.principal_id
        viewer_principal = await session.get(Principal, principal_id)
        assert viewer_principal is not None
        session_row = await session.get(SessionRow, session_id)
        agenda_md = session_row.agenda_md if session_row is not None else None
    # Ephemeral "who is preparing a reply" cue for live viewers: event_seq -1 marks it
    # non-durable (never a session_event row, never replayed on reconnect) -- pure
    # stream furniture, mirroring how token chunks work.
    if on_event is not None:
        await on_event(-1, "typing", {"persona_id": str(persona_id), "name": persona_name})

    # The transcript budget belongs to whichever model is about to read it.
    turn_params = await merged_persona_params(tenant_id, persona_id)
    raw_budget = turn_params.get(HISTORY_CHAR_BUDGET_KEY)
    try:
        history_budget_chars = int(raw_budget) if raw_budget is not None else None
    except (TypeError, ValueError):
        history_budget_chars = None
    conversation = await _load_conversation(
        tenant_id, session_id, principal_id, history_budget_chars
    )
    query_text = conversation[-1]["content"] if conversation else ""

    embeddings = await embedding_provider.embed(
        EmbedRequest(model=embedding_provider.model_name, texts=[query_text or " "])
    )
    query_embedding = embeddings[0]

    current_profile = await get_current_behavior_profile(tenant_id, persona_id)
    directives_text = ""
    axis_definitions: list[Any] = []
    if current_profile is not None:
        axis_definitions = await list_axis_definitions(tenant_id, current_profile.pack_id)
        directives_text = render_directives_for_profile(
            axis_definitions, current_profile.axis_values
        )

    # S1 (E2.5+E2.6): the disclosure gate runs BEFORE assembly whenever the phase grants
    # secret visibility and the acting principal holds any. Fail-closed inside the gate;
    # a factory failure here concedes nothing (no decisions -> exclusion-by-default,
    # concealed plaintext simply never enters selection). INV-1: the factory lives in
    # core/assembler and hands back opaque decisions -- this module touches no secrets
    # repo. `concealed_secrets` feeds the post-generation leak check (E2.7).
    resolved_secret_decisions: tuple[Any, ...] = ()
    concealed_secrets: tuple[Any, ...] = ()
    async with tenant_scope(tenant_id) as session:
        persona_agent = await session.get(Agent, persona.agent_id)
        workspace_row = await session.get(Workspace, workspace_id)
        workspace_settings = dict(workspace_row.settings) if workspace_row else {}
    # Secret handling is a per-workspace trust level, on phases whose pack declared
    # the capability (visibility.secrets = held_by_actor):
    #   excluded (default) -- held secrets never enter context. Leak-proof, dramaless.
    #   trust             -- the holder's own briefs enter its context, directive and
    #                        all, and the acting model plays them. Zero extra calls;
    #                        for models smart enough to keep character.
    #   gate              -- a per-turn classifier decides conceal/hint/reveal and the
    #                        verdict is enforced by exclusion. One extra call per
    #                        secret-holding turn; for models you do not trust with the
    #                        plaintext, and for tables where a reveal must update
    #                        who-knows-what.
    # The legacy boolean maps onto this: secrets_gate=true meant "gate".
    secret_mode = str(
        workspace_settings.get("secret_mode")
        or ("gate" if workspace_settings.get("secrets_gate") else "excluded")
    )
    if (
        encryptor is not None
        and secret_mode == "trust"
        and getattr(phase.visibility, "secrets", "none") == "held_by_actor"
    ):
        from core.assembler.secrets_gate_factory import resolve_trusted_secrets

        try:
            resolved_secret_decisions, concealed_secrets = await resolve_trusted_secrets(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                event_seq=event_seq,
                holder_principal_id=principal_id,
                persona_id=persona_id,
                behavior_profile_version=(current_profile.version if current_profile else 0),
                phase=phase,
                encryptor=encryptor,
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed to "no disclosure"
            structlog.get_logger().warning("secrets.trust_mode_failed", error=str(exc)[:300])
            resolved_secret_decisions, concealed_secrets = (), ()
    elif (
        encryptor is not None
        and secret_mode == "gate"
        and getattr(phase.visibility, "secrets", "none") == "held_by_actor"
    ):
        from core.assembler.secrets_gate_factory import resolve_turn_secrets

        gate_agent = persona_agent
        if gate_agent is not None:
            try:
                resolved_secret_decisions, concealed_secrets = await resolve_turn_secrets(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    event_seq=event_seq,
                    holder_principal_id=principal_id,
                    persona_id=persona_id,
                    persona_summary=(persona.persona_md or "")[:600],
                    phase=phase,
                    phase_label=phase_key,
                    recent_turns=[m["content"] for m in conversation[-6:]],
                    recent_turns_embedding=query_embedding,
                    axis_values=(dict(current_profile.axis_values) if current_profile else {}),
                    behavior_profile_version=(current_profile.version if current_profile else 0),
                    agent=gate_agent,
                    provider=model_provider_factory(gate_agent.provider),
                    encryptor=encryptor,
                    model_provider_factory=model_provider_factory,
                    axis_definitions=tuple(axis_definitions),
                    eval_arm=eval_arm,
                )
            except Exception as exc:  # noqa: BLE001 -- fail closed to "no disclosure"
                structlog.get_logger().warning("secrets.gate_factory_failed", error=str(exc)[:300])
                resolved_secret_decisions, concealed_secrets = (), ()

    # G4.1: when the phase RESERVES history budget (BudgetSpec.history_ratio > 0), the
    # elapsed transcript beyond the replayed tail is summarised (map-reduce, mechanical
    # facts rendered from records) and placed as one provenance-stamped block. Phases
    # that reserve nothing keep the old behaviour exactly: no summary, no model call.
    history_summary = None
    history_budget = phase.history_slice_tokens()
    if history_budget > 0 and persona_agent is not None and permission_service is not None:
        from core.sessions.history import summarise_history

        replayed_from = _replayed_from_seq(conversation, event_seq)
        if replayed_from > 0:
            try:
                summary = await summarise_history(
                    tenant_id,
                    workspace_id,
                    session_id,
                    viewer_principal,
                    phase,
                    from_event_seq=0,
                    to_event_seq=replayed_from - 1,
                    max_tokens=history_budget,
                    agent=persona_agent,
                    provider=model_provider_factory(persona_agent.provider),
                    permission_service=permission_service,
                )
                history_summary = summary.to_block()
            except Exception as exc:  # noqa: BLE001 -- a summary is an enrichment
                structlog.get_logger().warning("history.summarise_failed", error=str(exc)[:200])

    manifest = await assemble(
        viewer_principal,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text=query_text,
        query_embedding=query_embedding,
        # G4.1: the history section is governed by the phase's own declared slice
        # (BudgetSpec.history_ratio), not a hardcoded zero. Every already-authored
        # phase declares no ratio, so this is still 0 for all of them -- the number
        # now comes from the definition instead of from this call site.
        history_max_tokens=phase.history_slice_tokens(),
        behavior_directives_text=directives_text,
        resolved_secret_decisions=resolved_secret_decisions,
        disclosing_principal_id=principal_id,
        event_seq=event_seq,
        history_summary=history_summary,
        reranker=reranker,
    )
    manifest_row = await write_context_manifest(
        tenant_id,
        session_id,
        event_seq,
        principal_id,
        phase_key,
        manifest,
        behavior_profile_version=current_profile.version if current_profile else None,
    )

    tool_registry = ToolRegistry()
    # Caller-supplied tools (in-process drivers only -- e.g. the eval runner's lab
    # oracle). HTTP paths never populate this; phase.tools remains the workflow's gate
    # for everything a normal deployment offers.
    for spec, handler in extra_tools or ():
        tool_registry.register(spec, handler)
    if "randomizer" in phase.tools:
        handler = make_randomizer_handler(
            rule_system=rule_system,
            rule_system_id=rule_system_id,
            legal_check_types=rule_system.check_types,
            actor_fields_resolver=_make_actor_fields_resolver(tenant_id),
        )
        tool_registry.register(
            ToolSpec(
                name="randomizer",
                description=(
                    "Resolve an expression against a rule system and record the result. "
                    "Defaults to this workspace's rule system; pass `rule_system` to "
                    "resolve in another one it has registered (e.g. a coin flip)."
                ),
                parameters=RANDOMIZER_DEFINITION.input_schema,
            ),
            handler,
        )
    # P1: entity creation + resolved consequences. Gated on the phase granting the tool AND a
    # permission service being wired (the underlying writes run the real entity:create /
    # entity:mutate check on the acting persona -- rule 12).
    if permission_service is not None and "entity_create" in phase.tools:
        tool_registry.register(
            ToolSpec(
                name="entity_create",
                description=(
                    "Create one entity instance. Call it once per entity -- there is no "
                    "limit on how many you create. Any field you omit gets a sensible "
                    "default."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "schema_key": {
                            "type": "string",
                            "description": "which entity schema to instantiate",
                        },
                        "name": {"type": "string", "description": "the entity's display name"},
                        "fields": {
                            "type": "object",
                            "description": "field values; omit any you don't care about",
                        },
                        "bind_to_self": {
                            "type": "boolean",
                            "description": (
                                "true only when this entity represents you -- the record "
                                "you act through. You may have at most one. Leave it out "
                                "when creating anything else."
                            ),
                        },
                    },
                    "required": ["schema_key", "name"],
                },
            ),
            make_entity_create_handler(
                workspace_id=workspace_id, permission_service=permission_service
            ),
        )
    if permission_service is not None and "resolve_and_apply" in phase.tools:
        tool_registry.register(
            ToolSpec(
                name="resolve_and_apply",
                description=(
                    "Resolve a check against the rule system and apply its outcome to an "
                    "entity: set fields, then drive one of its state-machine transitions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "actor_entity_id": {"type": "string", "description": "uuid of the entity"},
                        "machine_key": {
                            "type": "string",
                            "description": "the state machine to drive, e.g. 'health'",
                        },
                        "trigger": {
                            "type": "string",
                            "description": "the transition trigger, e.g. 'damage_taken'",
                        },
                        "set_fields": {
                            "type": "object",
                            "description": "field changes to apply before the transition",
                        },
                        "expression": {
                            "type": "string",
                            "description": "randomizer expression for the check, default '1d2'",
                        },
                    },
                    "required": ["actor_entity_id", "machine_key", "trigger"],
                },
            ),
            make_resolve_apply_handler(
                workspace_id=workspace_id,
                rule_system=rule_system,
                rule_system_id=rule_system_id,
                permission_service=permission_service,
            ),
        )

    # Per-persona internet search: the persona's switch AND the workspace's registered
    # web_search MCP server (the egress control) must both be on. Not phase-gated -- the
    # actor-level switch is the policy (see core.process.session_web_tools).
    if (
        mcp_transport is not None
        and persona_web_search
        and await workspace_has_web_search(tenant_id, workspace_id)
    ):
        tool_registry.register(
            ToolSpec(
                name="web_search",
                description=(
                    "Search the internet for current information. Returns top results "
                    "with title, URL and snippet."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "what to search for"}
                    },
                    "required": ["query"],
                },
            ),
            make_web_search_handler(workspace_id=workspace_id, transport=mcp_transport),
        )

    # The workspace's registered deterministic resolution tools (the `resolution`
    # MCP preset a workflow's capabilities enable -- e.g. a pack's die roller):
    # server-side randomness, persisted ResolutionRecords (INV-7). Gated by the
    # workspace registration alone, like web search.
    if mcp_transport is not None:
        from core.process.session_resolution_tools import (
            make_resolution_handler,
            resolution_tools_for_workspace,
        )

        resolution_specs = await resolution_tools_for_workspace(
            tenant_id, workspace_id, transport=mcp_transport
        )
        resolution_names = [spec.name for spec in resolution_specs]
        for spec in resolution_specs:
            tool_registry.register(
                spec,
                make_resolution_handler(
                    workspace_id=workspace_id,
                    tool_name=spec.name,
                    all_tool_names=resolution_names,
                    transport=mcp_transport,
                ),
            )

    # Registered REMOTE MCP servers (admin-attached tenant grants or pack-declared
    # external endpoints): their allowed tools become native in-turn tools through
    # the same client-side enforcement (M-C). Workspace registration is the gate.
    if mcp_transport is not None:
        from core.process.session_remote_tools import (
            make_remote_tool_handler,
            remote_tools_for_workspace,
        )

        remote_specs = await remote_tools_for_workspace(
            tenant_id, workspace_id, transport=mcp_transport
        )
        allowed_remote = _phase_remote_allowlist(phase)
        if allowed_remote is not None:
            remote_specs = [(k, s) for k, s in remote_specs if s.name in allowed_remote]
        remote_names = [spec.name for _key, spec in remote_specs]
        for server_key, spec in remote_specs:
            tool_registry.register(
                spec,
                make_remote_tool_handler(
                    workspace_id=workspace_id,
                    server_key=server_key,
                    tool_name=spec.name,
                    all_tool_names=remote_names,
                    transport=mcp_transport,
                ),
            )

    system_blocks = [manifest.rendered_context]
    # The phase's own task instruction (DSL `prompt`): what this turn is FOR and what
    # shape its output should take -- without it the model only knows who it is, not
    # what the flow wants from it here.
    phase_prompt = getattr(phase, "prompt", "") or ""
    if phase_prompt.strip():
        system_blocks.append(f"Instructions for this phase:\n\n{phase_prompt.strip()}")
    # #5: the supervisor is the one who moves the discussion/flow along the agenda.
    if persona_type == "supervisor" and agenda_md and agenda_md.strip():
        system_blocks.append(
            "You are the facilitator of this session. Steer the discussion and advance "
            "the flow toward this agenda, keeping the conversation on track:\n\n"
            f"{agenda_md.strip()}"
        )
    # The minimal identity line is CORE mechanics: it derives from how this module
    # serializes the multi-actor transcript ("Name: ..." user turns), so every model
    # needs it regardless of workflow. Everything stylistic beyond it is content --
    # the workspace's own conduct_rules (editable in the UI) and the flow's phase
    # prompt, never hardcoded here.
    system_blocks.append(
        f"You are {persona_name} and you speak only as {persona_name}. Lines in the "
        "transcript prefixed with another name are that person speaking, not you."
    )
    conduct_rules = str(workspace_settings.get("conduct_rules") or "").strip()
    if conduct_rules:
        system_blocks.append(f"Conduct rules for this workspace:\n\n{conduct_rules}")
    messages: list[dict[str, object]] = [
        {"role": "system", "content": block} for block in system_blocks
    ]
    messages.extend(dict(turn) for turn in conversation)
    if not any(m["role"] == "user" for m in conversation):
        # Chat-completions backends (e.g. ollama's chat API, which litellm routes to
        # whenever tools are registered) reject a request with no user-role message.
        # Two ways to get there: the session's first turn (empty transcript), and a
        # persona whose history so far is only its OWN past turns (perspective mapping
        # keeps those 'assistant'). A neutral floor-holding line covers both.
        messages.append({"role": "user", "content": "(You have the floor.)"})

    # S2 (E2.7): the reply is checked against this turn's CONCEALED secrets before it
    # is ever persisted -- regenerate once with a nudge, then fall back to an in-voice
    # deflection + overseer alert. No concealed secrets -> passthrough closure -> the
    # common path costs nothing.
    _base_finalize = None
    if (concealed_secrets or moderation_provider is not None) and persona_agent is not None:
        from core.moderation.hooks import scan_generated
        from core.secrets.leak_check import run_post_generation_check

        async def _reserve_side_seq() -> int:
            async with tenant_scope(tenant_id) as session:
                row = await session.get(SessionRow, session_id)
                assert row is not None
                side_seq = max(row.next_event_seq, event_seq + 1)
                row.next_event_seq = side_seq + 1
                return side_seq

        async def _regenerate_once() -> str:
            nudged: list[dict[str, object]] = [
                *messages,
                {
                    "role": "system",
                    "content": (
                        "Your previous draft revealed information you are supposed to "
                        "keep to yourself. Reply again in character WITHOUT stating, "
                        "paraphrasing, or strongly implying that information."
                    ),
                },
            ]
            provider = model_provider_factory(persona_agent.provider)
            from core.tenancy.egress import load_egress_policy

            request = GenerationRequest(
                model=f"{persona_agent.provider}/{persona_agent.model}",
                messages=nudged,
                purpose="generation",
                api_base=persona_agent.api_base,
                params=dict(persona_agent.params or {}),
                egress_policy=await load_egress_policy(tenant_id),
            )
            parts: list[str] = []
            async for chunk in provider.generate(request):
                if chunk.text:
                    parts.append(chunk.text)
            return "".join(parts)

        async def _base_finalize(reply_text: str) -> str:
            checked = reply_text
            if concealed_secrets:
                outcome = await run_post_generation_check(
                    tenant_id,
                    session_id,
                    await _reserve_side_seq(),
                    checked,
                    concealed_secrets,
                    embedding_provider=embedding_provider,
                    regenerate=_regenerate_once,
                )
                checked = outcome.final_content
            if moderation_provider is not None:
                checked, _mod = await scan_generated(
                    tenant_id,
                    session_id,
                    await _reserve_side_seq(),
                    checked,
                    actor_principal_id=principal_id,
                    provider=moderation_provider,
                    regenerate=_regenerate_once,
                )
            return checked

    finalize_reply = _base_finalize

    try:
        result = await run_agent_turn(
            tenant_id,
            persona_id,
            session_id,
            messages,
            model_provider_factory=model_provider_factory,
            tool_registry=tool_registry,
            # The persona is part of the key: two personas conducted concurrently peek the
            # same next_event_seq, and a persona-less key made the second turn's TOOL
            # dispatches dedupe into the first's cached results (wrong actor's roll).
            idempotency_key=f"turn:{session_id}:{event_seq}:{persona_id}",
            encryptor=encryptor,
            context_manifest_id=manifest_row.id,
            on_chunk=on_chunk,
            event_seq=event_seq,
            finalize_reply=finalize_reply,
            triggered_by=triggered_by,
        )
    except UsageLimitExceededError as exc:
        # Same clean-pause guarantee: the session parks with the limit message rather
        # than crashing mid-advance; it resumes normally after the window resets.
        raise InterpreterFaultError(str(exc)) from exc
    except (AllRetriesExhaustedError, ToolLoopExceededError) as exc:
        # Restores advance_session's "never a stuck lock, pause cleanly" guarantee
        # for this case -- neither exception is an InterpreterFaultError on its own,
        # so without this translation they'd propagate unhandled past advance_session.
        raise InterpreterFaultError(str(exc)) from exc

    if on_event is not None and result.message_id is not None:
        async with tenant_scope(tenant_id) as session:
            author_name = await resolve_author_name(session, tenant_id, principal_id)
        await on_event(
            event_seq,
            "message",
            {
                "id": str(result.message_id),
                "role": "assistant",
                "content": result.content_md,
                "author": author_name,
                "persona_id": str(persona_id),
                "created_at": datetime.now(UTC).isoformat(),
                "triggered_by": triggered_by,
            },
        )

    return ActorTurnResult(
        content_md=result.content_md,
        already_persisted=True,
        message_id=result.message_id,
    )


def _make_execute_turn(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    model_provider_factory: ModelProviderFactory,
    embedding_provider: EmbeddingProvider,
    reranker: Reranker | None = None,
    rule_system: RuleSystemDefinition,
    rule_system_id: uuid.UUID,
    on_chunk: OnChunk | None,
    encryptor: Encryptor | None = None,
    permission_service: PermissionService | None = None,
    mcp_transport: McpTransport | None = None,
    moderation_provider: ModerationProvider | None = None,
    on_event: OnEvent | None = None,
) -> Callable[[ActorRef, InterpreterContext], Awaitable[ActorTurnResult]]:
    async def execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        # 'free' (human-typed) and 'generate_as' (human-typed-in-an-agent's-place) both
        # need content from a real HTTP POST, not something synthesized here -- see
        # HumanTurnPendingError's own docstring for the scheduler-cursor mechanics this
        # relies on.
        if actor.mode in ("free", "generate_as"):
            raise HumanTurnPendingError()

        async with tenant_scope(tenant_id) as session:
            persona = await session.scalar(
                select(Persona).where(
                    Persona.tenant_id == tenant_id, Persona.principal_id == actor.principal_id
                )
            )
            if persona is None:
                raise InterpreterFaultError(
                    f"actor {actor.principal_id} is not a known persona in workspace {workspace_id}"
                )
            persona_id = persona.id

        return await run_one_persona_turn(
            triggered_by="scheduler",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            session_id=ctx.session_id,
            persona_id=persona_id,
            phase=ctx.phase,
            phase_key=ctx.phase_key,
            event_seq=ctx.event_seq,
            model_provider_factory=model_provider_factory,
            embedding_provider=embedding_provider,
            reranker=reranker,
            rule_system=rule_system,
            rule_system_id=rule_system_id,
            encryptor=encryptor,
            on_chunk=on_chunk,
            permission_service=permission_service,
            mcp_transport=mcp_transport,
            moderation_provider=moderation_provider,
            # The typing cue and the rich completed-message mirror both live inside
            # run_one_persona_turn and both hang off this. Omitting it meant the
            # autonomous scheduler -- the path every process-definition session actually
            # runs on -- streamed every turn with no cue at all, so the UI could not say
            # who was speaking and labelled every in-flight reply with the facilitator
            # fallback ("Arbiter" under the rpg overlay).
            on_event=on_event,
        )

    return execute_turn


def _make_conduct_gated_scheduler(inner: NextActorFn, tenant_id: uuid.UUID) -> NextActorFn:
    """#7: wraps the real scheduler so a session's live ``turn_policy`` decides whether the
    conductable discussion phase auto-runs or waits for a human.

    In ``directed`` mode, on a phase flagged ``conductable``:
      * not yet wrapped up -> raise ``HumanTurnPendingError`` (before the inner scheduler is
        even consulted, so its cursor is untouched) -> ``advance_session`` returns
        ``awaiting_human`` and parks. The overseer conducts turns out of band
        (``run_directed_persona_turn`` / the G4.4 override), none of which advance the phase.
      * wrapped up (``conductor_wrap_up`` set) -> return ``None`` so the interpreter, finding
        no actor and no ``await`` on the phase, evaluates the gates and jumps to synthesis.
    In ``auto`` mode, or on any non-conductable phase, it delegates to the real scheduler
    unchanged (autonomous multi-round)."""

    async def next_actor(ctx: InterpreterContext) -> ActorRef | None:
        if "conductable" in ctx.phase.flags:
            async with tenant_scope(tenant_id) as session:
                row = await session.get(SessionRow, ctx.session_id)
                policy = row.turn_policy if row is not None else "auto"
            if policy == "directed":
                if ctx.state.get("conductor_wrap_up"):
                    return None
                raise HumanTurnPendingError()
        return await inner(ctx)

    return next_actor


async def run_process_definition_session(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    definition: ProcessDefinitionDSL,
    *,
    model_provider_factory: ModelProviderFactory,
    embedding_provider: EmbeddingProvider,
    reranker: Reranker | None = None,
    on_chunk: OnChunk | None = None,
    on_event: OnEvent | None = None,
    encryptor: Encryptor | None = None,
    permission_service: PermissionService | None = None,
    mcp_transport: McpTransport | None = None,
    moderation_provider: ModerationProvider | None = None,
) -> AdvanceResult:
    """The entry point the HTTP layer calls (once at message-submit time, and again
    after ``core.process.interpreter.submit_human_turn`` records a human's reply)."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        workspace_id = row.workspace_id

    rule_system_row = await get_or_create_default_rule_system(tenant_id)
    rule_system = RuleSystemDefinition.from_row(rule_system_row)
    await ensure_tool_definition(tenant_id, RANDOMIZER_DEFINITION)

    next_actor_fn = _make_conduct_gated_scheduler(
        make_scheduler(
            make_default_candidate_resolver(
                workspace_id,
                persona_candidate_resolver=make_persona_candidate_resolver(tenant_id, workspace_id),
            )
        ),
        tenant_id,
    )
    execute_turn = _make_execute_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        model_provider_factory=model_provider_factory,
        embedding_provider=embedding_provider,
        reranker=reranker,
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        encryptor=encryptor,
        on_chunk=on_chunk,
        permission_service=permission_service,
        mcp_transport=mcp_transport,
        moderation_provider=moderation_provider,
        on_event=on_event,
    )

    return await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=next_actor_fn,
        execute_turn=execute_turn,
        checkpoint_hook=make_checkpoint_hook(workspace_id),
        # G4.3: the definition carries the pacing defaults an individual phase's await
        # may inherit -- passing it here is what makes reminders reach a real session.
        on_await=make_await_hook(definition),
        on_event=on_event,
    )


class DirectedTurnError(Exception):
    """#7: a directed (human-conducted) turn could not run -- e.g. the persona isn't in the
    session's roster, or the session runs no process definition (nothing to draw a phase's
    visibility/context budget from). Distinct from an ``InterpreterFaultError``: this is a
    caller/setup error the HTTP layer turns into a 4xx, not a paused session."""


async def run_directed_persona_turn(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    persona_id: uuid.UUID,
    definition: ProcessDefinitionDSL,
    *,
    model_provider_factory: ModelProviderFactory,
    embedding_provider: EmbeddingProvider,
    reranker: Reranker | None = None,
    on_chunk: OnChunk | None = None,
    on_event: OnEvent | None = None,
    encryptor: Encryptor | None = None,
    permission_service: PermissionService | None = None,
    mcp_transport: McpTransport | None = None,
    moderation_provider: ModerationProvider | None = None,
) -> uuid.UUID:
    """#7 managed flow: run exactly one model-generated turn for a human-chosen persona in a
    conducted session, then stop (unlike ``run_process_definition_session``, it never advances
    the phase -- the overseer paces the discussion). Reuses ``run_one_persona_turn``, so the
    turn's context/leak/usage/idempotency handling is identical to an autonomous one.

    The session stays parked at its conductable phase throughout; this only appends a message
    at the next ``event_seq``. Safe as a single writer: in ``directed`` mode the autonomous
    loop is parked (the conduct-gated scheduler raised ``HumanTurnPendingError``), so nothing
    else is claiming sequence numbers concurrently."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise DirectedTurnError(f"no session {session_id} in this tenant")
        workspace_id = row.workspace_id
        phase_key = row.current_phase
        event_seq = row.next_event_seq
        roster_match = await session.scalar(
            select(SessionPersonaRow.id).where(
                SessionPersonaRow.session_id == session_id,
                SessionPersonaRow.persona_id == persona_id,
            )
        )
    if roster_match is None:
        raise DirectedTurnError(f"persona {persona_id} is not in session {session_id}'s roster")

    phase = definition.phases.get(phase_key)
    if phase is None:
        raise DirectedTurnError(
            f"session is in phase {phase_key!r}, which its pinned definition does not declare"
        )

    rule_system_row = await get_or_create_default_rule_system(tenant_id)
    rule_system = RuleSystemDefinition.from_row(rule_system_row)
    await ensure_tool_definition(tenant_id, RANDOMIZER_DEFINITION)

    result = await run_one_persona_turn(
        # A person picked this persona and asked for the turn -- that is what the
        # transcript's trigger chip means by "conducted". This read an undefined local
        # until now, so every conducted turn raised NameError before it reached the
        # model: Managed mode looked like it did nothing.
        triggered_by="conducted",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        persona_id=persona_id,
        phase=phase,
        phase_key=phase_key,
        event_seq=event_seq,
        model_provider_factory=model_provider_factory,
        embedding_provider=embedding_provider,
        reranker=reranker,
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        encryptor=encryptor,
        on_chunk=on_chunk,
        permission_service=permission_service,
        mcp_transport=mcp_transport,
        moderation_provider=moderation_provider,
        # typing cue + completed-message mirror now live INSIDE run_one_persona_turn,
        # the single emit point every driver (HTTP, worker, benchmark runner) shares.
        on_event=on_event,
    )

    assert result.message_id is not None
    return result.message_id
