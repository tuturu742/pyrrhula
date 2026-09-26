"""CCv3 card export, lossy and honest about it.

**Import: day one. Export: best-effort with loss warnings. Native format: never.** A card
cannot carry a behaviour profile, a secret, a process definition, or a per-phase budget --
there are no fields for them and inventing some would produce a file no other tool
understands while claiming compatibility. So this writes what CCv3 can hold, puts what it
can't into `extensions.pyrrhula` (which the spec reserves and requires implementations not
to destroy), and produces an **itemised loss report** the user sees *before* download.

The loss report is not a courtesy. Exporting a workspace's agent to a card and handing it
to someone is a moment where a person forms a belief about what they just shared. A silent
lossy export lets them believe they shared everything.

**Secrets are excluded unconditionally.** Card export is not one of the export *modes* --
there is no "full card". It is always sanitised-equivalent for secret content, and
`tests/leak/test_card_export.py` scans the produced bytes to prove it. The reasoning is
that a `.pyr` bundle goes to someone the exporter chose deliberately with a mode they had
to pick; a card goes into an ecosystem of sharing sites, and the least surprising thing a
card can do is carry no secrets at all.

Both `chara` and `ccv3` chunks are written: card-writing frontends do the same for backward
compatibility, and a card that only older tools can read is a card half the ecosystem
can't.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from core.agents.models import Persona
from core.behavior.repo import list_behavior_profile_versions
from core.knowledge.models import KnowledgeEntry
from core.portability.ccv3.map import EXTENSIONS_KEY, list_source_entries
from core.portability.ccv3.png_chunks import encode_card_payload, write_text_chunks
from core.process.authoring import list_definitions
from core.secrets.models import SecretRow
from core.tenancy.scope import tenant_scope

SPEC_VERSION = "3.0"
PYRRHULA_EXTENSION_KEY = "pyrrhula"


@dataclass(frozen=True)
class LossItem:
    """One thing the card cannot carry. ``carried_in_extension`` distinguishes "gone" from
    "gone as far as other tools are concerned, but restorable by re-importing here" --
    a distinction a user needs to make a decision, and one a single "lossy!" warning
    destroys."""

    kind: str
    detail: str
    carried_in_extension: bool

    def render(self) -> str:
        suffix = (
            " (kept in extensions.pyrrhula; a Pyrrhula re-import restores it)"
            if self.carried_in_extension
            else " (not representable in CCv3 at all)"
        )
        return f"{self.kind}: {self.detail}{suffix}"


@dataclass
class LossReport:
    items: list[LossItem] = field(default_factory=list)

    def add(self, kind: str, detail: str, *, carried: bool) -> None:
        self.items.append(LossItem(kind=kind, detail=detail, carried_in_extension=carried))

    def to_json(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": i.kind,
                "detail": i.detail,
                "carried_in_extension": i.carried_in_extension,
            }
            for i in self.items
        ]

    def render(self) -> str:
        if not self.items:
            return "Nothing was lost: this agent has no data CCv3 cannot represent."
        return "\n".join(f"- {item.render()}" for item in self.items)


@dataclass(frozen=True)
class CardExportResult:
    payload: dict[str, Any]
    loss_report: LossReport

    def to_png(self, image: bytes) -> bytes:
        """Both chunks, same payload. `ccv3` is what a modern reader takes; `chara` keeps
        older tooling working, which is the ecosystem convention and what makes a card
        actually portable rather than nominally so."""
        encoded = encode_card_payload(self.payload)
        return write_text_chunks(image, {"ccv3": encoded, "chara": encoded})


async def export_agent_as_card(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, persona_id: uuid.UUID
) -> CardExportResult:
    """The inverse of the mapping, sharing its model and its `EXTENSIONS_KEY` -- one
    constant for both directions, so they cannot disagree about where native data lives."""
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        if agent is None or agent.workspace_id != workspace_id:
            raise ValueError(f"no agent {persona_id} in workspace {workspace_id}")
        session.expunge(agent)

    raw_settings = agent.settings or {}
    raw_stored = raw_settings.get(EXTENSIONS_KEY, {})
    stored: dict[str, Any] = dict(raw_stored) if isinstance(raw_stored, dict) else {}
    loss = LossReport()

    entries: list[KnowledgeEntry] = []
    source_ref = stored.get("knowledge_source_id")
    if source_ref:
        entries = await list_source_entries(tenant_id, uuid.UUID(str(source_ref)))

    entry_extensions = dict(stored.get("entries", {}))
    # Quarantined entries are omitted entirely: exporting content no human has cleared
    # would launder a quarantine through a file format. The loss report says so.
    book_entries = [
        _entry_to_book_entry(e, entry_extensions.get(e.entry_key, {}))
        for e in entries
        if not e.quarantined
    ]

    persona = _split_persona(agent.persona_md)
    payload: dict[str, Any] = {
        "spec": "chara_card_v3",
        "spec_version": SPEC_VERSION,
        "data": {
            "name": agent.name,
            "description": persona.get("Description", ""),
            "personality": persona.get("Personality", ""),
            "scenario": persona.get("Scenario", ""),
            "first_mes": persona.get("Opening message", ""),
            "mes_example": persona.get("Example dialogue", ""),
            "system_prompt": persona.get("System prompt", ""),
            "post_history_instructions": persona.get("Post-history instructions", ""),
            "alternate_greetings": list(stored.get("alternate_greetings", [])),
            "tags": list(stored.get("tags", [])),
            "creator": str(stored.get("creator", "")),
            "creator_notes": "",
            "character_version": "",
            "extensions": {
                **dict(stored.get("card", {})),
                PYRRHULA_EXTENSION_KEY: {},  # filled in below, once loss is known
            },
            "character_book": {"name": f"{agent.name} lore", "entries": book_entries},
        },
    }

    native = await _collect_native_data(tenant_id, workspace_id, persona_id, entries, loss)
    payload["data"]["extensions"][PYRRHULA_EXTENSION_KEY] = {
        **native,
        "loss_report": loss.to_json(),
    }
    return CardExportResult(payload=payload, loss_report=loss)


async def _collect_native_data(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    persona_id: uuid.UUID,
    entries: list[KnowledgeEntry],
    loss: LossReport,
) -> dict[str, Any]:
    """Everything CCv3 has no field for. Two outcomes only: it goes into
    `extensions.pyrrhula` and the loss report says "restorable", or it does not and the
    loss report says "gone". There is deliberately no third, silent outcome."""
    native: dict[str, Any] = {"schema": 1, "persona_type": None, "activation": {}}

    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        assert agent is not None
        native["persona_type"] = agent.persona_type

    profiles = await list_behavior_profile_versions(tenant_id, persona_id)
    if profiles:
        native["behavior_profile"] = {
            "version": profiles[0].version,
            "pack_id": profiles[0].pack_id,
            "axis_values": dict(profiles[0].axis_values),
        }
        loss.add(
            "behaviour profile",
            f"{len(profiles)} version(s); CCv3 has no field for behavioural axes",
            carried=True,
        )

    # Activation fields CCv3 cannot express: `constant` and `position` map onto the spec,
    # but Pyrrhula's sticky/cooldown/delay/trigger_pct/inclusion_group do not.
    unmappable = {
        e.entry_key: {
            "sticky": e.sticky,
            "cooldown": e.cooldown,
            "delay": e.delay,
            "trigger_pct": e.trigger_pct,
            "inclusion_group": e.inclusion_group,
            "class": e.class_,
            "scope_key": e.scope_key,
        }
        for e in entries
        if any((e.sticky, e.cooldown, e.delay, e.trigger_pct, e.inclusion_group))
    }
    if unmappable:
        native["activation"] = unmappable
        loss.add(
            "activation fields",
            f"{len(unmappable)} entry/entries use sticky/cooldown/delay/trigger_pct/"
            "inclusion_group, which CCv3 decorators cannot express",
            carried=True,
        )

    secret_count = await _count_secrets(tenant_id, workspace_id)
    if secret_count:
        loss.add(
            "secrets",
            f"{secret_count} secret(s) in this workspace are excluded unconditionally -- "
            "card export is always sanitised-equivalent and is not an export mode",
            carried=False,
        )

    definitions = await list_definitions(tenant_id, workspace_id=workspace_id)
    if definitions:
        loss.add(
            "process definitions",
            f"{len(definitions)} definition(s); a card describes one participant, not a "
            "process, and CCv3 has no representation for phases, gates, or budgets",
            carried=False,
        )
        loss.add(
            "per-phase budgets",
            "token budgets and retrieval ratios live on process phases and go with them",
            carried=False,
        )

    quarantined = [e.entry_key for e in entries if e.quarantined]
    if quarantined:
        loss.add(
            "quarantined entries",
            f"{len(quarantined)} entry/entries are quarantined pending review and are "
            "omitted from the card entirely",
            carried=False,
        )

    return native


async def _count_secrets(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
    """Counts, never reads. This module has no path to `secret.content` at all -- the count
    is what the loss report needs, and a count cannot leak a fact."""
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(SecretRow.id).where(SecretRow.workspace_id == workspace_id)
                )
            ).scalars()
        )
    return len(rows)


def _entry_to_book_entry(entry: KnowledgeEntry, extension: dict[str, Any]) -> dict[str, Any]:
    """One `character_book` entry. Quarantined entries never reach here (the caller filters
    them) -- exporting content a human has not cleared would launder a quarantine through
    a file format."""
    decorators = dict(extension.get("decorators", {}))
    content = entry.body_md
    if decorators:
        header = "\n".join(f"@@{name} {value}".rstrip() for name, value in decorators.items())
        content = f"{header}\n{content}"

    return {
        "name": entry.title,
        "keys": list(entry.keys or []),
        "secondary_keys": list(entry.secondary_keys or []),
        "selective": entry.logic == "AND",
        "constant": entry.constant,
        "insertion_order": entry.insertion_order,
        "enabled": bool(extension.get("enabled", True)),
        # CCv3 knows two positions; Pyrrhula's `at_depth_N` has no equivalent and falls
        # back rather than emitting a token other readers would reject.
        "position": (
            entry.position if entry.position in ("before_char", "after_char") else "before_char"
        ),
        "use_regex": entry.use_regex,
        "content": content,
        "extensions": dict(extension.get("extensions", {})),
    }


def _split_persona(persona_md: str) -> dict[str, str]:
    """The inverse of `map._render_persona`: split back on the `## Heading` blocks it
    wrote. A persona a human edited by hand may not have those headings, in which case the
    whole text becomes `Description` -- lossy, but visibly so, and better than dropping
    text because it didn't match a shape."""
    sections: dict[str, str] = {}
    current = "Description"
    buffer: list[str] = []
    for line in persona_md.splitlines():
        if line.startswith("## "):
            if buffer:
                sections[current] = "\n".join(buffer).strip()
            current = line.removeprefix("## ").strip()
            buffer = []
        else:
            buffer.append(line)
    if buffer:
        sections[current] = "\n".join(buffer).strip()
    return sections
