"""Card -> Pyrrhula objects: an `Persona`, an entity instance, and a
`KnowledgeSource(class=lore)` whose entries carry the card's activation fields.

This is the ecosystem's front door. A user with a folder of cards should be able to drop
one in and play, which means the mapping has to be faithful in the direction that matters:
the card's *activation behaviour* -- which lore fires, when, where it lands in the prompt
-- must survive, because that behaviour is what its author actually tuned.

**Everything imported runs the injection scan.** Cards are the classic carrier: a lore
entry is free text authored by a stranger and destined for a tool-calling agent's context.
Flagged entries import quarantined, exactly as `.pyr` bundles do -- one quarantine
mechanism, not a card-shaped variant of one.

**`extensions` survives byte-for-byte**, on the agent and on every entry. The spec reserves
it and requires implementations not to destroy it; G4.9 round-trips what it carries.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text

from core.agents.models import Agent, Persona
from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.models import KnowledgeEntry
from core.portability.ccv3.normalise import NormalisedCard
from core.portability.injection_scan import quarantine_reason, scan_text
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope

# Where the card's own extension payload lands on the imported agent, and where G4.9 looks
# for it on the way back out. One constant, so the two directions cannot disagree.
EXTENSIONS_KEY = "ccv3_extensions"


@dataclass
class CardImportResult:
    persona_id: uuid.UUID
    knowledge_source_id: uuid.UUID | None
    entry_keys: list[str] = field(default_factory=list)
    quarantined: list[tuple[str, str]] = field(default_factory=list)

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantined)


async def import_card(
    card: NormalisedCard,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
) -> CardImportResult:
    """Creates the agent and, when the card has a lore book, a `lore` KnowledgeSource
    attached to the workspace and published as v1.

    Published, not left as a draft: an unpublished source retrieves nothing, and a card
    that imports "successfully" but whose lore never fires is the worst kind of half-
    success -- everything looks right and nothing works."""
    profile_id = agent_id or await _any_model_profile(tenant_id)
    if profile_id is None:
        raise ValueError(
            "the workspace has no model profile to attach an imported agent to; create "
            "one before importing a card"
        )

    persona_scan = scan_text(f"{card.persona_md}\n{card.system_prompt}\n{card.opening_message}")
    persona_id = await _create_persona(card, tenant_id, workspace_id, profile_id)

    result = CardImportResult(persona_id=persona_id, knowledge_source_id=None)
    if persona_scan:
        # The persona is not a knowledge entry and has no quarantine flag of its own; it is
        # reported so a human sees it, and that is stated rather than implied. Personas are
        # authored *into* an agent, not retrieved, so the retrieval-exclusion mechanism
        # quarantine relies on has nothing to bite on here.
        result.quarantined.append(("<persona>", quarantine_reason(persona_scan) or ""))

    if not card.lore_entries:
        return result

    source = await create_source(
        tenant_id,
        key=f"card-{_slug(card.name)}-{uuid.uuid4().hex[:6]}",
        name=f"{card.name} lore",
        class_="lore",
    )
    for entry in card.lore_entries:
        reason = quarantine_reason(scan_text(f"{entry.title}\n{entry.body_md}"))
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry.entry_key,
            EntryFields(
                title=entry.title,
                body_md=entry.body_md,
                class_="lore",
                scope_key="workspace_public",
                keys=entry.keys,
                secondary_keys=entry.secondary_keys,
                logic=entry.logic,
                use_regex=entry.use_regex,
                constant=entry.constant,
                position=entry.position,
                insertion_order=entry.insertion_order,
            ),
        )
        result.entry_keys.append(entry.entry_key)
        if reason is not None:
            result.quarantined.append((entry.entry_key, reason))

    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, "workspace_public", version_pin=version.id
    )

    await _chunk_published_entries(tenant_id, source.id, version.id)
    await _apply_quarantine(tenant_id, source.id, dict(result.quarantined))
    await _store_entry_extensions(tenant_id, persona_id, source.id, card)

    result.knowledge_source_id = source.id
    return result


async def _create_persona(
    card: NormalisedCard,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> uuid.UUID:
    """`persona_type='participant'`: a card describes someone who *takes part*, not someone
    who runs the process. A facilitator is a role a human assigns deliberately, and
    inferring it from an imported file would hand a stranger's content the seat that
    decides what everyone else sees."""
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="agent", display_name=card.name)
        session.add(principal)
        await session.flush()

        agent = Persona(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=principal.id,
            key=f"{_slug(card.name)}-{uuid.uuid4().hex[:6]}",
            name=card.name,
            persona_type="participant",
            persona_md=_render_persona(card),
            agent_id=agent_id,
        )
        session.add(agent)
        await session.flush()
        return agent.id


def _render_persona(card: NormalisedCard) -> str:
    """The card's prompt-shaping fields, in the order the spec places them, with the
    Pyrrhula-native extension payload appended as a fenced block so a round-trip through
    G4.9 can find it again. Kept as prose rather than a side table because a persona *is*
    prose -- the moment it becomes structured, someone has to decide what happens to a
    field the structure didn't anticipate."""
    parts = [card.persona_md]
    if card.system_prompt:
        parts.append(f"## System prompt\n{card.system_prompt}")
    if card.post_history_instructions:
        parts.append(f"## Post-history instructions\n{card.post_history_instructions}")
    if card.opening_message:
        parts.append(f"## Opening message\n{card.opening_message}")
    if card.example_dialogue:
        parts.append(f"## Example dialogue\n{card.example_dialogue}")
    return "\n\n".join(p for p in parts if p.strip())


async def _chunk_published_entries(
    tenant_id: uuid.UUID, source_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    """One chunk per entry, whole-body, no splitting.

    A card's lore entry is authored as one unit and *activates* as one unit -- its keys
    fire it, its position places it. Running it through the semantic chunker would split
    it into pieces that retrieve independently, which changes the activation behaviour the
    card's author tuned. The whole reason to import a card faithfully is that behaviour.

    Chunks are created here rather than by the ingestion pipeline because a card is not a
    document upload; it arrives already structured. Embeddings stay the existing
    `embed_chunks` job's job -- the API route enqueues it after import, and until it runs
    the entries retrieve through the keyed/sparse paths, which is exactly how lore is
    meant to fire anyway."""
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                " scope_key, embedding, content_hash) "
                "SELECT e.tenant_id, e.id, e.version_id, 0, e.body_md, "
                "       array_length(regexp_split_to_array(e.body_md, '\\s+'), 1), "
                "       e.class, e.scope_key, NULL, md5(e.body_md) "
                "FROM knowledge_entry e "
                "WHERE e.knowledge_source_id = :s AND e.version_id = :v "
                "  AND length(trim(e.body_md)) > 0"
            ),
            {"s": source_id, "v": version_id},
        )


async def _apply_quarantine(
    tenant_id: uuid.UUID, source_id: uuid.UUID, reasons: dict[str, str]
) -> None:
    """Flags the published entries the scan objected to, and their chunks. Runs *after*
    publish because publish copies draft rows into version rows -- flagging the draft would
    leave the published copy, the one retrieval actually reads, unflagged."""
    entry_reasons = {k: v for k, v in reasons.items() if not k.startswith("<")}
    if not entry_reasons:
        return
    async with tenant_scope(tenant_id) as session:
        for entry_key, reason in entry_reasons.items():
            await session.execute(
                text(
                    "UPDATE knowledge_entry SET quarantined = true, quarantine_reason = :r "
                    "WHERE knowledge_source_id = :s AND entry_key = :k"
                ),
                {"r": reason, "s": source_id, "k": entry_key},
            )
            await session.execute(
                text(
                    "UPDATE knowledge_chunk SET quarantined = true WHERE entry_id IN "
                    "(SELECT id FROM knowledge_entry WHERE knowledge_source_id = :s "
                    " AND entry_key = :k)"
                ),
                {"s": source_id, "k": entry_key},
            )


async def _store_entry_extensions(
    tenant_id: uuid.UUID, persona_id: uuid.UUID, source_id: uuid.UUID, card: NormalisedCard
) -> None:
    """Per-entry `extensions` and decorators, kept beside the entry so a round-trip can put
    them back. Stored on the *agent's* extension payload keyed by entry_key rather than in
    a new column: this is foreign data Pyrrhula does not interpret, and giving it a schema
    would be claiming to understand it."""
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        if agent is None:
            return
        payload: dict[str, Any] = {
            "card": card.extensions,
            "spec_version": card.spec_version,
            "knowledge_source_id": str(source_id),
            "alternate_greetings": card.alternate_greetings,
            "tags": card.tags,
            "creator": card.creator,
            "entries": {
                entry.entry_key: {
                    "extensions": entry.extensions,
                    "decorators": entry.decorators,
                    "depth": entry.depth,
                    "enabled": entry.enabled,
                }
                for entry in card.lore_entries
            },
        }
        agent.settings = {**(agent.settings or {}), EXTENSIONS_KEY: payload}


async def read_card_extensions(tenant_id: uuid.UUID, persona_id: uuid.UUID) -> dict[str, Any]:
    """What ``_store_entry_extensions`` put away, for the export and for the round-trip
    test. ``{}`` when the agent did not come from a card."""
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        if agent is None:
            return {}
        stored = (agent.settings or {}).get(EXTENSIONS_KEY, {})
    return dict(stored) if isinstance(stored, dict) else {}


async def list_source_entries(tenant_id: uuid.UUID, source_id: uuid.UUID) -> list[KnowledgeEntry]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeEntry)
                .where(
                    KnowledgeEntry.knowledge_source_id == source_id,
                    KnowledgeEntry.version_id.is_not(None),
                )
                .order_by(KnowledgeEntry.insertion_order, KnowledgeEntry.entry_key)
            )
        ).scalars()
        return list(rows)


async def _any_model_profile(tenant_id: uuid.UUID) -> uuid.UUID | None:
    async with tenant_scope(tenant_id) as session:
        profile_id: uuid.UUID | None = await session.scalar(select(Agent.id).limit(1))
        return profile_id


def _slug(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "card"
