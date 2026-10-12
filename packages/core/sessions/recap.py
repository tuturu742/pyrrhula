"""Phase recaps: what happened in a phase, for one viewer, written from records only.

When a phase ends, each participant's replayed conversation gets a short recap of it at
that point -- the votes, the rolls, what changed, what that participant was told, who
spoke with whom, what knowledge it was shown, its own tool calls. A flow chooses the
sections (``RecapSpec``); a section with nothing in it is left out, so one flow's recap
is votes and another's is rolls and hints, from the same mechanism.

Three rules, the same ones the history summary keeps (``core.sessions.history``):

1. **Records, never prose about them.** Every line is rendered from a row. No model is
   asked anything, so a recap cannot be wrong about what the record says, and costs
   nothing to make.
2. **Per viewer, under the visibility its context already has.** Facts come through
   ``collect_visible_facts`` (scope-filtered entity changes, disclosures only to whom
   they were made); messages through the audience predicate in the query; votes through
   the vote's own reveal rule; knowledge from the viewer's own context manifests. A
   recap tells a viewer nothing its context did not already admit.
3. **No secret text.** A disclosure is recapped as who told the viewer, how, and the
   words of the message it was said in -- words the viewer heard. Nothing here can
   reach ``secret.content`` (INV-1 keeps the secrets repo out of this module).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select

from core.agents.models import Persona
from core.assembler.models import ContextManifestRow
from core.entities.storage import EntityRow
from core.ports.permission import PermissionService
from core.process import participants as parts
from core.process.dsl.schema import PhaseSpec, ProcessDefinitionDSL, RecapSpec
from core.resolution.records import ResolutionRecordRow
from core.secrets.models import SecretDisclosureEventRow
from core.sessions.history import MechanicalFact, collect_visible_facts
from core.sessions.models import (
    MessageRow,
    SessionEventRow,
    SessionPersonaRow,
)
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope

_EXCERPT = 220
_DISCLOSURE_VERBS = {
    "full": "told you",
    "hint": "hinted to you",
    "inferred": "let you work out",
    "leaked": "let slip to you",
}


@dataclass(frozen=True)
class PhaseSpan:
    """One ended phase: everything with ``start < event_seq <= end`` happened in it, and
    its recap is placed at ``end`` (the transition that ended it)."""

    key: str
    label: str
    start: int
    end: int


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _EXCERPT else text[: _EXCERPT - 1] + "…"


def _mentions(text: str, names: Iterable[str]) -> list[str]:
    """Roster names a message mentions -- whole words, full name or any part of three or
    more letters, so initials and particles never count."""
    found = []
    for name in names:
        words = [name, *(w for w in name.split() if len(w) >= 3)]
        if any(re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) for w in words):
            found.append(name)
    return found


def _bucket(seq: int | None, spans: Sequence[PhaseSpan]) -> PhaseSpan | None:
    if seq is None:
        return None
    for span in spans:
        if span.start < seq <= span.end:
            return span
    return None


async def build_recaps(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer: Principal,
    viewer_name: str,
    viewer_is_supervisor: bool,
    phase: PhaseSpec,
    definition: ProcessDefinitionDSL,
    spec: RecapSpec,
    spans: Sequence[PhaseSpan],
    permission_service: PermissionService | None,
) -> dict[int, str]:
    """The recap of each ended phase in ``spans``, keyed by the seq it is placed at.
    Phases with nothing to recap are absent."""
    if not spans:
        return {}
    wanted = set(spec.sections)
    first, last = min(s.start for s in spans), max(s.end for s in spans)
    lines: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    def add(seq: int | None, section: str, line: str) -> None:
        span = _bucket(seq, spans)
        if span is not None:
            lines[span.end][section].append(line)

    async with tenant_scope(tenant_id) as session:
        roster = {
            str(pid): name
            for pid, name in (
                await session.execute(
                    select(Persona.principal_id, Persona.name)
                    .join(SessionPersonaRow, SessionPersonaRow.persona_id == Persona.id)
                    .where(SessionPersonaRow.session_id == session_id)
                )
            ).all()
        }

        if "votes" in wanted:
            for seq, payload in (
                await session.execute(
                    select(SessionEventRow.event_seq, SessionEventRow.payload).where(
                        SessionEventRow.session_id == session_id,
                        SessionEventRow.kind == "ballot_result",
                        SessionEventRow.event_seq > first,
                        SessionEventRow.event_seq <= last,
                    )
                )
            ).all():
                if not parts.result_readable(payload, viewer.id):
                    continue
                rendered = parts.render_ballot_result(
                    payload,
                    viewer_is_supervisor=viewer_is_supervisor,
                    viewer_principal_id=viewer.id,
                )
                add(seq, "votes", rendered.removeprefix("- "))

        if "conversation" in wanted:
            spoken: dict[int, dict[str, tuple[int, set[str]]]] = defaultdict(dict)
            rows = (
                await session.execute(
                    select(
                        MessageRow.event_seq, MessageRow.author_principal_id, MessageRow.content_md
                    )
                    .where(
                        MessageRow.session_id == session_id,
                        MessageRow.event_seq > first,
                        MessageRow.event_seq <= last,
                        or_(
                            MessageRow.audience.is_(None),
                            MessageRow.audience.contains([viewer.id]),
                        ),
                    )
                    .order_by(MessageRow.event_seq)
                )
            ).all()
            for seq, author_id, text in rows:
                span = _bucket(seq, spans)
                author = roster.get(str(author_id))
                if span is None or author is None:
                    continue
                others = [n for n in roster.values() if n != author]
                count, addressed = spoken[span.end].get(author, (0, set()))
                spoken[span.end][author] = (
                    count + 1,
                    addressed | set(_mentions(text or "", others)),
                )
            for end, by_author in spoken.items():
                parts_ = []
                for author, (count, addressed) in by_author.items():
                    who = "you" if author == viewer_name else author
                    to = ", ".join(sorted("you" if a == viewer_name else a for a in addressed))
                    parts_.append(f"{who} {count}×" + (f" (naming {to})" if to else ""))
                lines[end]["conversation"].append("; ".join(parts_))

        if "tools" in wanted:
            for seq, payload in (
                await session.execute(
                    select(SessionEventRow.event_seq, SessionEventRow.payload).where(
                        SessionEventRow.session_id == session_id,
                        SessionEventRow.kind == "tool_call",
                        SessionEventRow.event_seq > first,
                        SessionEventRow.event_seq <= last,
                    )
                )
            ).all():
                if payload.get("author") != viewer_name:
                    continue
                args = ", ".join(f"{k}={v}" for k, v in (payload.get("arguments") or {}).items())
                add(
                    seq,
                    "tools",
                    f"your {payload.get('tool_name')}({_excerpt(args)}): {payload.get('outcome')}",
                )

        if "knowledge" in wanted:
            seen: set[str] = set()
            for seq, entries in (
                await session.execute(
                    select(ContextManifestRow.event_seq, ContextManifestRow.entries)
                    .where(
                        ContextManifestRow.session_id == session_id,
                        ContextManifestRow.viewer_principal_id == viewer.id,
                        ContextManifestRow.event_seq <= last,
                    )
                    .order_by(ContextManifestRow.event_seq)
                )
            ).all():
                fresh = []
                for entry in entries or []:
                    key = entry.get("entry_key")
                    if not key or entry.get("class") == "rules" or key in seen:
                        continue
                    seen.add(key)
                    fresh.append(str(key))
                if fresh:
                    add(seq, "knowledge", "first shown to you: " + ", ".join(fresh))

    facts: tuple[MechanicalFact, ...] = ()
    if {"rolls", "changes", "disclosures"} & wanted and permission_service is not None:
        facts = await collect_visible_facts(
            tenant_id,
            workspace_id,
            session_id,
            viewer,
            phase,
            from_event_seq=first + 1,
            to_event_seq=last,
            permission_service=permission_service,
        )
    rolls = [f for f in facts if f.kind == "resolution"]
    if rolls and "rolls" in wanted:
        actors = await _roll_actors(tenant_id, [f.record_id for f in rolls])
        for f in rolls:
            who = actors.get(f.record_id) or "a roll"
            add(
                f.event_seq,
                "rolls",
                f"{who}: {f.fields.get('expression')} = "
                f"{f.fields.get('total')}, {f.fields.get('outcome')}",
            )
    if "changes" in wanted:
        for f in facts:
            if f.kind == "entity_change":
                add(
                    f.event_seq,
                    "changes",
                    f"{f.fields.get('entity')}: {f.fields.get('field')} "
                    f"{f.fields.get('from')} → {f.fields.get('to')}",
                )
    disclosures = [f for f in facts if f.kind == "disclosure"]
    if disclosures and "disclosures" in wanted:
        for seq, line in await _disclosure_lines(tenant_id, viewer.id, roster, disclosures):
            add(seq, "disclosures", line)

    labels = {s.end: s.label for s in spans}
    return {
        end: _render(labels[end], sections, spec.sections)
        for end, sections in lines.items()
        if any(sections.values())
    }


_SECTION_TITLES = {
    "votes": "Votes",
    "rolls": "Rolls",
    "changes": "Changes",
    "disclosures": "What you were told",
    "conversation": "Who spoke",
    "knowledge": "Knowledge",
    "tools": "Your tool calls",
}


def _render(label: str, sections: Mapping[str, list[str]], order: Sequence[str]) -> str:
    out = [f"Recap of {label}, from the record:"]
    for name in order:
        for line in sections.get(name, []):
            out.append(f"- {_SECTION_TITLES[name]}: {line}")
    return "\n".join(out)


async def _roll_actors(tenant_id: uuid.UUID, record_ids: list[str]) -> dict[str, str]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(ResolutionRecordRow.id, EntityRow.name)
                .join(EntityRow, EntityRow.id == ResolutionRecordRow.actor_entity_id, isouter=True)
                .where(ResolutionRecordRow.id.in_([uuid.UUID(r) for r in record_ids]))
            )
        ).all()
    return {str(rid): name for rid, name in rows if name}


async def _disclosure_lines(
    tenant_id: uuid.UUID,
    viewer_id: uuid.UUID,
    roster: Mapping[str, str],
    facts: Sequence[MechanicalFact],
) -> list[tuple[int | None, str]]:
    """Who told the viewer, how, and the words they said it in -- never the secret."""
    ids = [uuid.UUID(f.record_id) for f in facts]
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(SecretDisclosureEventRow, MessageRow.content_md)
                .join(
                    MessageRow, MessageRow.id == SecretDisclosureEventRow.message_id, isouter=True
                )
                .where(SecretDisclosureEventRow.id.in_(ids))
                .order_by(SecretDisclosureEventRow.event_seq)
            )
        ).all()
    out: list[tuple[int | None, str]] = []
    for row, said in rows:
        by = roster.get(str(row.disclosed_by_principal_id), "someone")
        told = row.disclosed_to.get("principal_ids", [])
        told_ids = told if isinstance(told, list) else []
        if str(viewer_id) in told_ids:
            line = f"{by} {_DISCLOSURE_VERBS.get(row.mode, 'told you')}"
        else:  # an inspector's view: the fact of it, not as if it were told to them
            line = f"{by} disclosed ({row.mode}) to {len(told_ids)} participant(s)"
        if said:
            line += f': "{_excerpt(said)}"'
        out.append((row.event_seq, line))
    return out


def spans_from_transitions(
    transitions: Sequence[tuple[int, Mapping[str, Any]]],
    initial_phase: str,
    labels: Mapping[str, str],
) -> list[PhaseSpan]:
    """The ended phases, from the session's phase_transition events in order."""
    spans: list[PhaseSpan] = []
    start, current = -1, initial_phase
    for seq, payload in transitions:
        spans.append(PhaseSpan(current, labels.get(current, current.replace("_", " ")), start, seq))
        start = seq
        to = payload.get("to")
        current = to if isinstance(to, str) else current
    return spans
