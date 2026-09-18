"""ContextAssembler (C1.2, plan §4.1/§6.3, INV-1/2/8) — the ONLY code path from stored
text to a model's context. Everything else in the architecture is arranged around
protecting this one invariant (INV-1): only this module and ``core.overseer`` may import
``core.knowledge.repo``/``core.secrets.repo``.

**Frozen signature (INV-2, CLAUDE.md hard rule 3):** ``viewer`` and ``phase`` are the
first two parameters, required and defaultless -- omitting either is both a mypy error
(no ``Optional``, no default) and a runtime ``TypeError`` (explicit guard below, in case a
call site ever bypasses static typing).

The 10 steps of §6.3, and which task owns each:

1. **Scope** (C1.1) -- ``VisibilityResolver.scopes_for`` -- done, real.
2. **Budget split** (A1.6) -- ``phase.budget.ratio``/``.max_tokens`` -- done, real.
3-6. **Per-class hybrid retrieve, WRRF fuse, rerank, bucket fill** (A1.4-A1.7) --
   ``search_and_budget`` -- done, real.
7. **Entity state** (F3.6) -- ``entity_state_renderer`` is an injection seam, still
   defaulting to a no-op here (every live turn now calls ``assemble()`` via
   core/process/live_session.py -- same "not
   yet wired into the runtime" gap every other injection seam in this module
   documents). ``core.entities.injection.render_entity_state`` is the real
   implementation a caller passes explicitly; it isn't imported as the default here to
   avoid a module cycle (``core.entities`` already depends on this module for
   ``EntityStateBlock``'s shape).
8. **Secrets + disclosure gate** (E2.6, plan §8.4 (D4), INV-8) -- ``resolved_secret_
   decisions`` carries each relevant secret's already-resolved disposition (action +
   plaintext fields *only* populated when the action legally allows it -- see
   ``core.secrets.exclusion.ResolvedSecretDecision``), resolved by the caller from
   ``core.secrets.gate``'s output before calling ``assemble()`` (same pattern as
   ``behavior_directives_text``, E2.4: the model call and the repo reads that produce a
   resolved decision happen upstream, not inside this function). Per decision,
   ``core.secrets.exclusion.render_injection`` decides what text (if any) enters the
   volatile block, and a ``reveal_full`` additionally commits the disclosure event +
   holder update atomically (``core.secrets.exclusion.apply_reveal``) -- "exclusion at
   selection, not scrubbing after": a concealed secret's ``content`` is never
   constructed as a candidate string here at all, since ``ResolvedSecretDecision.content``
   is `None` for anything but ``reveal_full``. ``secrets_gate`` (below) is a distinct,
   still-no-op seam for a different, narrower concern (scrubbing retrieved *knowledge*
   text that happens to restate a secret verbatim) that no task has actually needed yet;
   don't conflate the two.
9. **Layout stable->volatile** (C1.4, plan §6.3 step 9/§16.1) -- entity state and
   constant-why knowledge entries (unchanging within a session) go in
   ``LayoutSections.stable``; ranked/retrieved (non-constant) knowledge, recent history,
   and rendered secret injections (different every turn -- a gate decision can flip
   turn to turn as pressure changes, so this can never be cache-stable the way
   ``behavior_directives_text`` is) go in ``.volatile``. Enforced structurally by
   ``core.assembler.layout.LayoutSections``, not by string-concatenation order that a
   future edit could quietly get backwards. ``behavior_directives_text`` (E2.4) is
   pre-rendered banded prompt-directive text for the acting agent's current behavior
   profile -- resolved by the caller (``core.process.live_session``, which already
   fetches the profile for the manifest's ``behavior_profile_version``), not here; it
   belongs in ``stable`` because it only changes when the profile version does, same as
   entity state.
10. **Manifest** -- built here as a plain in-memory dataclass (not yet persisted --
    persistence + replay is C1.3's job).

**Determinism.** Given a fixed corpus (pinned knowledge versions), a fixed query, and
``reranker=None`` (skips the one call this module can't otherwise guarantee is
deterministic in this environment), two calls to ``assemble()`` produce byte-identical
``rendered_context``/``content_hash`` -- proven by ``test_context_assembler.py``. Citation
ids (``k1``, ``k2``, ...) are assigned by final render order, itself fully determined by
WRRF rank + bucket fill, so they're stable too.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from core.assembler.layout import LayoutSections
from core.assembler.visibility import scopes_for
from core.knowledge import repo as knowledge_repo
from core.knowledge.activation import ActivatedEntry, EntryActivationState, activate_entries
from core.knowledge.retrieval.assemble import search_and_budget
from core.knowledge.retrieval.budget import BudgetedChunk
from core.knowledge.retrieval.cache import RetrievalCache
from core.knowledge.retrieval.priority import class_priority_weights
from core.knowledge.retrieval.rerank import fetch_chunk_texts
from core.observability.otel import get_tracer
from core.ports.reranker import Reranker
from core.ports.scope import ScopeSet
from core.process.dsl.schema import PhaseSpec
from core.secrets.exclusion import ResolvedSecretDecision, apply_reveal, render_injection
from core.sessions.models import MessageRow
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope

_tracer = get_tracer(__name__)


@dataclass(frozen=True)
class Redaction:
    type: str
    id: str
    reason: str


@dataclass(frozen=True)
class ManifestEntry:
    """One included knowledge chunk, citable in the rendered context as ``[k<n>]``."""

    citation_id: str
    chunk_id: uuid.UUID
    entry_id: uuid.UUID
    entry_key: str
    source_id: uuid.UUID
    version_id: uuid.UUID | None
    class_: str
    bucket: str
    rank: int
    score: float
    why: str
    token_count: int


@dataclass(frozen=True)
class ContextManifest:
    id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    rendered_context: str
    # C1.4: the same text as ``rendered_context``, split at the prompt-cache boundary --
    # ``stable_prefix + volatile_suffix == rendered_context`` (with the same single blank
    # -line join `LayoutSections.render()` uses). Exposed separately so a caller building a
    # real provider request can mark exactly this boundary as cacheable.
    stable_prefix: str
    volatile_suffix: str
    entries: tuple[ManifestEntry, ...]
    redactions: tuple[Redaction, ...]
    resolution_ids: tuple[uuid.UUID, ...]
    token_counts: dict[str, int]
    content_hash: str
    # F3.6: exactly which entity versions were injected this turn -- ``{entity_id:
    # version}``. Persisted onto ``context_manifest.entity_versions``/``checkpoint
    # .entity_versions`` (both existed as ``{}``-always placeholders since C1.3/B1.4),
    # so a replayed/forked turn can tell which entity state it was generated against.
    entity_versions: dict[str, int] = field(default_factory=dict)
    # G4.1 (INV-10 across a resume): which elapsed-history range the injected summary
    # covered, and the hash of the summary text itself. ``None`` on a turn that injected
    # no summary -- the overwhelmingly common case, and every pre-G4.1 caller.
    history_summary_from_seq: int | None = None
    history_summary_to_seq: int | None = None
    history_summary_hash: str | None = None


@dataclass(frozen=True)
class EntityStateBlock:
    rendered_text: str
    token_count: int
    entity_versions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class HistorySummaryBlock:
    """G4.1's pre-rendered elapsed-history summary, resolved by the caller exactly the
    way ``EntityStateBlock``/``behavior_directives_text`` are -- ``core.sessions.history
    .summarise_history`` builds it (a worker job, a model call, and per-viewer visibility
    filtering, none of which belong inside the assembler), this module only places it and
    records its provenance.

    ``content_hash`` and ``(from_event_seq, to_event_seq)`` land on the manifest so a
    resumed turn replays: re-running ``assemble()`` needs the same summary text, and the
    manifest is what says which one that was."""

    rendered_text: str
    token_count: int
    from_event_seq: int
    to_event_seq: int
    content_hash: str


EntityStateRenderer = Callable[
    [uuid.UUID, uuid.UUID, uuid.UUID, Principal, PhaseSpec], Awaitable[EntityStateBlock]
]
SecretsGateHook = Callable[
    [str, Principal, PhaseSpec, uuid.UUID], Awaitable[tuple[str, tuple[Redaction, ...]]]
]


async def _default_entity_state_renderer(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer: Principal,
    phase: PhaseSpec,
) -> EntityStateBlock:
    """MVP: no entity system exists yet (F3.6, Phase 3) -- renders nothing."""
    del tenant_id, workspace_id, session_id, viewer, phase
    return EntityStateBlock(rendered_text="", token_count=0)


async def _noop_secrets_gate(
    rendered_knowledge: str, viewer: Principal, phase: PhaseSpec, session_id: uuid.UUID
) -> tuple[str, tuple[Redaction, ...]]:
    """MVP: no SecretsService exists yet (E2.6, Phase 2) -- passes through unchanged."""
    del viewer, phase, session_id
    return rendered_knowledge, ()


def citation_envelope(
    citation_id: str, class_: str, source_name: str, entry_title: str, body: str
) -> str:
    """§6.5's citation envelope. Contents are DATA, never instructions -- the standing
    system rule that makes this safe against knowledge-borne prompt injection lives in the
    agent's system prompt (outside this module's scope), not in the envelope shape itself.

    Public (G4.5) so ``core.portability.replay`` rebuilds a turn's knowledge block from a
    bundle using *this* envelope rather than a copy of it. A second implementation of the
    envelope would make the cross-boundary replay test pass by agreeing with itself."""
    return (
        f'<knowledge id="{citation_id}" class="{class_}" source="{source_name}" '
        f'entry="{entry_title}">\n{body}\n</knowledge>'
    )


def token_proxy(rendered: str) -> int:
    """Token proxy for sections with no precomputed token_count (history, entity state,
    G4.1's history summary) -- coarse, but consistent with the ± one chunk/message
    tolerance the budget acceptance criterion already allows for. Knowledge chunks use
    their real, ingestion-time ``token_count`` (A1.2) instead, not this proxy.

    Public (G4.1) so ``core.sessions.history`` sizes a summary with the *same* proxy this
    module then charges it against the history budget with -- two different counters
    either side of that hand-off would make the budget arithmetic quietly wrong."""
    return len(rendered.split())


class HistoryBudgetExceededError(Exception):
    """G4.1: an injected ``HistorySummaryBlock`` claims more tokens than the turn's whole
    history budget allows. Loud rather than silently truncated -- the summariser owns
    fitting the summary to the budget it was given (``core.sessions.history
    .summarise_history(max_tokens=...)``), and a mismatch here means a caller passed a
    summary built against a different budget than the one it is now being placed in."""


async def _render_history(
    tenant_id: uuid.UUID, session_id: uuid.UUID, max_tokens: int, *, after_event_seq: int | None
) -> tuple[str, int]:
    """Walks backward from the newest message, stopping at the token cap, then renders
    oldest-to-newest so the transcript reads naturally -- the most recent turns are kept,
    not the earliest, when the window doesn't fit everything.

    ``after_event_seq`` (G4.1) excludes messages a resume summary already covers: including
    them verbatim *and* in the summary would double-charge the same history against the
    budget and hand the model the same turn twice. ``None`` = no summary, include
    everything (every pre-G4.1 caller)."""
    async with tenant_scope(tenant_id) as session:
        stmt = select(MessageRow).where(MessageRow.session_id == session_id)
        if after_event_seq is not None:
            stmt = stmt.where(MessageRow.event_seq > after_event_seq)
        rows = list((await session.execute(stmt.order_by(MessageRow.event_seq.desc()))).scalars())

    included: list[MessageRow] = []
    used = 0
    for row in rows:
        cost = token_proxy(row.content_md)
        if used + cost > max_tokens:
            break
        included.append(row)
        used += cost

    included.reverse()
    rendered = "\n".join(f"{m.role}: {m.content_md}" for m in included)
    return rendered, used


async def _render_knowledge(
    tenant_id: uuid.UUID, budgeted_chunks: list[BudgetedChunk]
) -> tuple[list[str], list[str], tuple[ManifestEntry, ...]]:
    """Splits rendered knowledge blocks into stable (``why == "constant"`` -- always
    active regardless of query, so identical across every turn of a session) and volatile
    (ranked/retrieved -- differs by query) for C1.4's layout contract. Citation ids are
    still assigned by final render order (stable entries first) so they stay stable too."""
    chunk_texts = await fetch_chunk_texts(tenant_id, [c.chunk_id for c in budgeted_chunks])
    source_names = await knowledge_repo.get_source_names(
        tenant_id, list({c.source_id for c in budgeted_chunks})
    )
    entry_titles = await knowledge_repo.get_entry_titles(
        tenant_id, list({c.entry_id for c in budgeted_chunks})
    )

    ordered = sorted(budgeted_chunks, key=lambda c: (c.why != "constant", c.class_, c.rank))
    entries: list[ManifestEntry] = []
    stable_blocks: list[str] = []
    volatile_blocks: list[str] = []
    for i, chunk in enumerate(ordered, start=1):
        citation_id = f"k{i}"
        block = citation_envelope(
            citation_id,
            chunk.class_,
            source_names.get(chunk.source_id, str(chunk.source_id)),
            entry_titles.get(chunk.entry_id, chunk.entry_key),
            chunk_texts.get(chunk.chunk_id, ""),
        )
        (stable_blocks if chunk.why == "constant" else volatile_blocks).append(block)
        entries.append(
            ManifestEntry(
                citation_id=citation_id,
                chunk_id=chunk.chunk_id,
                entry_id=chunk.entry_id,
                entry_key=chunk.entry_key,
                source_id=chunk.source_id,
                version_id=chunk.version_id,
                class_=chunk.class_,
                bucket=chunk.bucket,
                rank=chunk.rank,
                score=chunk.score,
                why=chunk.why,
                token_count=chunk.token_count,
            )
        )
    return stable_blocks, volatile_blocks, tuple(entries)


async def _activate_for_workspace(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    scope_set: ScopeSet,
    classes: list[str],
    scan_text: str,
    turn_index: int,
    session_id: uuid.UUID,
) -> dict[str, list[ActivatedEntry]]:
    """Which of this workspace's attached entries are eligible this turn, per class.

    Reads through ``core.knowledge.repo``, which only this module and the overseer may
    import (INV-1) -- which is also why activation belongs here rather than at the call
    site in ``core.process``: the process layer is not allowed to see entries at all.

    Scope is applied to the *entry*, so an entry outside the viewer's resolved scope set
    is never a candidate; the chunk-level filter downstream is a second, independent
    pass over the same rule.

    Activation state (``sticky``/``cooldown``) is loaded per session and written back, so
    those fields mean what they say. They were inert for as long as this ran with an empty
    prior state for as long as nothing persisted it (fixed alongside the
    entry_activation_state table)."""
    from sqlalchemy import select as sa_select

    from core.knowledge.activation import EntryActivationState
    from core.knowledge.models import (
        EntryActivationStateRow,
        KnowledgeSource,
        WorkspaceKnowledgeAttachment,
    )
    from core.knowledge.repo import list_published_entries
    from core.tenancy.scope import tenant_scope

    if not classes or not scope_set:
        return {}

    async with tenant_scope(tenant_id) as session:
        prior_rows = list(
            (
                await session.execute(
                    sa_select(EntryActivationStateRow).where(
                        EntryActivationStateRow.session_id == session_id
                    )
                )
            ).scalars()
        )
        prior_state = {
            str(row.entry_id): EntryActivationState(
                sticky_until_turn=row.sticky_until_turn,
                cooldown_until_turn=row.cooldown_until_turn,
            )
            for row in prior_rows
        }

    async with tenant_scope(tenant_id) as session:
        attachments = list(
            (
                await session.execute(
                    sa_select(WorkspaceKnowledgeAttachment, KnowledgeSource)
                    .join(
                        KnowledgeSource,
                        KnowledgeSource.id == WorkspaceKnowledgeAttachment.knowledge_source_id,
                    )
                    .where(
                        WorkspaceKnowledgeAttachment.workspace_id == workspace_id,
                        WorkspaceKnowledgeAttachment.enabled.is_(True),
                    )
                )
            ).all()
        )

    by_class: dict[str, list[ActivatedEntry]] = {}
    for attachment, source in attachments:
        # NULL pin means "follow the source's current version" (plan req 5).
        version_id = attachment.version_pin or source.current_version_id
        if version_id is None:
            continue
        entries = [
            entry
            for entry in await list_published_entries(tenant_id, version_id)
            if entry.scope_key in scope_set and not entry.quarantined
        ]
        if not entries:
            continue
        result = activate_entries(
            entries,
            scan_text=scan_text,
            turn_index=turn_index,
            prior_state=prior_state,
            rng_seed=f"{session_id}:{attachment.id}",
        )
        for activated in result.activated:
            entry = next(e for e in entries if e.id == activated.entry_id)
            if entry.class_ in classes:
                by_class.setdefault(entry.class_, []).append(activated)
        await _persist_activation_state(
            tenant_id, session_id, result.new_state, {e.id for e in entries}
        )
    return by_class


async def _persist_activation_state(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    new_state: dict[str, EntryActivationState],
    entry_ids: set[uuid.UUID],
) -> None:
    """Write back only this source's entries, upserting on (session, entry).

    Scoped to ``entry_ids`` because ``new_state`` carries forward every key it was given,
    including other sources' -- writing all of them from each source's pass would have
    each attachment fight the others for the same rows."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from core.knowledge.models import EntryActivationStateRow
    from core.tenancy.scope import tenant_scope

    rows = [
        {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "entry_id": uuid.UUID(key),
            "sticky_until_turn": state.sticky_until_turn,
            "cooldown_until_turn": state.cooldown_until_turn,
        }
        for key, state in new_state.items()
        if _as_uuid(key) in entry_ids
        and (state.sticky_until_turn is not None or state.cooldown_until_turn is not None)
    ]
    if not rows:
        return
    async with tenant_scope(tenant_id) as session:
        statement = pg_insert(EntryActivationStateRow).values(rows)
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_entry_activation_state",
                set_={
                    "sticky_until_turn": statement.excluded.sticky_until_turn,
                    "cooldown_until_turn": statement.excluded.cooldown_until_turn,
                },
            )
        )


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


async def assemble(
    viewer: Principal,
    phase: PhaseSpec,
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    query_text: str,
    query_embedding: Sequence[float],
    history_max_tokens: int,
    entity_state_renderer: EntityStateRenderer = _default_entity_state_renderer,
    secrets_gate: SecretsGateHook = _noop_secrets_gate,
    reranker: Reranker | None = None,
    cache: RetrievalCache | None = None,
    activated_entries_by_class: dict[str, list[ActivatedEntry]] | None = None,
    behavior_directives_text: str = "",
    resolved_secret_decisions: Sequence[ResolvedSecretDecision] = (),
    disclosing_principal_id: uuid.UUID | None = None,
    event_seq: int = 0,
    history_summary: HistorySummaryBlock | None = None,
) -> ContextManifest:
    if viewer is None:  # pragma: no cover -- type system already forbids this; INV-2 belt
        raise TypeError("ContextAssembler.assemble() requires a viewer (INV-2)")
    if phase is None:  # pragma: no cover -- same
        raise TypeError("ContextAssembler.assemble() requires a phase (INV-2)")

    with _tracer.start_as_current_span("assembler.assemble") as span:
        span.set_attribute("pyrrhula.viewer_id", str(viewer.id))
        span.set_attribute("pyrrhula.session_id", str(session_id))

        with _tracer.start_as_current_span("assembler.scope") as scope_span:
            scope_set = await scopes_for(
                tenant_id, viewer.id, workspace_id, phase.visibility, session_id
            )
            scope_span.set_attribute("pyrrhula.scope_count", len(scope_set))

        # G4.1: the history slice comes out of the phase budget *before* retrieval runs,
        # not as a truncation of what retrieval already spent -- see BudgetSpec
        # .history_ratio. `history_slice_tokens()` is 0 for every phase that never
        # declared one, so this subtraction is a no-op for all pre-G4.1 definitions.
        history_reserved_tokens = phase.history_slice_tokens()

        # Activation, if the caller did not supply it. The whole activation feature --
        # `constant`, keyword triggers, inclusion groups -- existed with no production
        # caller, so a knowledge entry marked "always include this" was included by
        # nothing. The symptom is not an error: retrieval simply finds nothing on the
        # first turn (an empty query matches no tsvector and an absent embedding matches
        # no vector), and a workspace full of lore behaves as though the agents had never
        # been briefed.
        #
        # Scope: `constant` and keyword activation work from here. `sticky` and `cooldown`
        # need per-session activation state that is not persisted anywhere yet, so this
        # passes an empty prior state and their bookkeeping is a no-op -- they degrade to
        # "activates when its keys match", never to something wrong.
        if activated_entries_by_class is None:
            activated_entries_by_class = await _activate_for_workspace(
                tenant_id,
                workspace_id,
                scope_set=scope_set,
                classes=list(phase.budget.ratio) if phase.budget is not None else [],
                scan_text=query_text,
                turn_index=event_seq,
                session_id=session_id,
            )

        budgeted_chunks: list[BudgetedChunk] = []
        if phase.budget is not None and scope_set:
            with _tracer.start_as_current_span("assembler.retrieve_and_budget") as retrieve_span:
                budgeted_chunks = await search_and_budget(
                    tenant_id,
                    scope_keys=scope_set,
                    query_embedding=query_embedding,
                    query_text=query_text,
                    class_ratios=phase.budget.ratio,
                    max_tokens=phase.budget.max_tokens - history_reserved_tokens,
                    activated_entries_by_class=activated_entries_by_class,
                    # The phase's ratio says what this *kind of turn* wants; the weights say
                    # what this *workspace* attached and how much it is worth here. Both
                    # matter, and only the first was ever applied.
                    priority_weights=await class_priority_weights(tenant_id, workspace_id),
                    spill=phase.budget.spill,
                    reranker=reranker,
                    cache=cache,
                )
                retrieve_span.set_attribute("pyrrhula.chunks_included", len(budgeted_chunks))

        with _tracer.start_as_current_span("assembler.entity_state"):
            entity_block = await entity_state_renderer(
                tenant_id, workspace_id, session_id, viewer, phase
            )

        with _tracer.start_as_current_span("assembler.render_knowledge"):
            stable_blocks, volatile_blocks, manifest_entries = await _render_knowledge(
                tenant_id, budgeted_chunks
            )
            stable_knowledge = "\n\n".join(stable_blocks)
            volatile_knowledge = "\n\n".join(volatile_blocks)

        with _tracer.start_as_current_span("assembler.secrets_gate"):
            # Applied separately to each side of the layout boundary (C1.4) so a gate
            # that redacts content doesn't collapse the stable/volatile split back into
            # one string -- see module docstring on why the split is structural, not
            # string-concatenation order.
            stable_knowledge, stable_redactions = await secrets_gate(
                stable_knowledge, viewer, phase, session_id
            )
            volatile_knowledge, volatile_redactions = await secrets_gate(
                volatile_knowledge, viewer, phase, session_id
            )
            redactions = stable_redactions + volatile_redactions

        with _tracer.start_as_current_span("assembler.secrets_exclusion") as exclusion_span:
            # E2.6: exclusion at selection, not scrubbing after -- render_injection never
            # has `content` to reach for unless the action is reveal_full, so a
            # concealed secret's fact is structurally never constructed as a candidate
            # string here.
            secrets_texts: list[str] = []
            secrets_redactions: list[Redaction] = []
            for resolved in resolved_secret_decisions:
                text, redaction = render_injection(resolved)
                if text:
                    secrets_texts.append(text)
                if redaction is not None:
                    secrets_redactions.append(
                        Redaction(type=redaction.type, id=redaction.id, reason=redaction.reason)
                    )
                if resolved.action == "reveal_full":
                    await apply_reveal(
                        tenant_id,
                        resolved,
                        session_id,
                        event_seq,
                        disclosed_by_principal_id=disclosing_principal_id,
                    )
            secrets_text = "\n\n".join(secrets_texts)
            redactions = redactions + tuple(secrets_redactions)
            exclusion_span.set_attribute(
                "pyrrhula.secrets_resolved", len(resolved_secret_decisions)
            )

        with _tracer.start_as_current_span("assembler.history") as history_span:
            summary_tokens = history_summary.token_count if history_summary is not None else 0
            if summary_tokens > history_max_tokens:
                raise HistoryBudgetExceededError(
                    f"history summary claims {summary_tokens} tokens but the turn's history "
                    f"budget is {history_max_tokens}"
                )
            rendered_history, raw_history_tokens = await _render_history(
                tenant_id,
                session_id,
                history_max_tokens - summary_tokens,
                after_event_seq=(
                    history_summary.to_event_seq if history_summary is not None else None
                ),
            )
            history_tokens = summary_tokens + raw_history_tokens
            history_span.set_attribute("pyrrhula.history_tokens", history_tokens)
            history_span.set_attribute("pyrrhula.history_summary_tokens", summary_tokens)

        summary_text = history_summary.rendered_text if history_summary is not None else ""
        sections = LayoutSections(
            stable=(behavior_directives_text, entity_block.rendered_text, stable_knowledge),
            volatile=(volatile_knowledge, summary_text, rendered_history, secrets_text),
        )
        rendered_context = sections.render()

        token_counts: dict[str, int] = {
            "entity_state": entity_block.token_count,
            "history": history_tokens,
        }
        for chunk in budgeted_chunks:
            token_counts[chunk.class_] = token_counts.get(chunk.class_, 0) + chunk.token_count
        token_counts["total"] = sum(token_counts.values())

        manifest = ContextManifest(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            rendered_context=rendered_context,
            stable_prefix=sections.stable_text,
            volatile_suffix=sections.volatile_text,
            entries=manifest_entries,
            redactions=redactions,
            resolution_ids=(),
            token_counts=token_counts,
            content_hash=hashlib.sha256(rendered_context.encode()).hexdigest(),
            entity_versions=dict(entity_block.entity_versions),
            history_summary_from_seq=(
                history_summary.from_event_seq if history_summary is not None else None
            ),
            history_summary_to_seq=(
                history_summary.to_event_seq if history_summary is not None else None
            ),
            history_summary_hash=(
                history_summary.content_hash if history_summary is not None else None
            ),
        )
        span.set_attribute("pyrrhula.manifest_tokens_total", token_counts["total"])
        span.set_attribute("pyrrhula.stable_prefix_tokens", len(sections.stable_text.split()))
        return manifest
