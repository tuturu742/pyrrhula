"""Citation validation: agents cite ``[k7]``-style
knowledge ids from the manifest's envelope (the ``ManifestEntry.citation_id``); a
cheap post-generation validator checks every cited id against the manifest that actually
produced the context the reply was generated from, so a hallucinated citation -- an id
the model made up, never actually present -- is caught and flagged rather than silently
trusted.

**Version pinning is what makes "why did the Arbiter rule that way" answerable forever.**
A citation records ``(entry_key, source_id, version_id)`` from the manifest entry at
generation time -- resolving it later (``resolve_citation``) always fetches that exact,
immutable published row, never whatever the source's current draft/latest version has
since become, even after further publishes change the entry's text.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from core.knowledge import repo as knowledge_repo
from core.knowledge.models import KnowledgeEntry
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope

_CITATION_RE = re.compile(r"\[(k\d+)\]")


@dataclass(frozen=True)
class Citation:
    citation_id: str
    entry_key: str
    source_id: uuid.UUID
    version_id: uuid.UUID | None


@dataclass(frozen=True)
class CitationValidationResult:
    valid_citations: tuple[Citation, ...]
    bad_citation_ids: tuple[str, ...]  # cited but absent from the manifest -- hallucinated
    missing_required_citation: bool  # requires_citation phase flag, zero valid citations


def extract_cited_ids(reply_text: str) -> list[str]:
    return _CITATION_RE.findall(reply_text)


def validate_citations(
    reply_text: str,
    manifest_entries: list[dict[str, object]],
    *,
    requires_citation: bool,
) -> CitationValidationResult:
    """``manifest_entries`` is ``ContextManifestRow.entries``'s stored JSON shape (see
    ``core.assembler.manifest._entry_to_json``) -- each dict carries ``citation_id``,
    ``entry_key``, ``source_id``, ``version_id`` as the assembler and manifest produce
    them."""
    cited_ids = extract_cited_ids(reply_text)
    entries_by_citation_id = {
        entry["citation_id"]: entry for entry in manifest_entries if "citation_id" in entry
    }

    valid: list[Citation] = []
    bad: list[str] = []
    seen_bad: set[str] = set()
    for cited_id in cited_ids:
        entry = entries_by_citation_id.get(cited_id)
        if entry is None:
            if cited_id not in seen_bad:
                bad.append(cited_id)
                seen_bad.add(cited_id)
            continue
        version_id = entry.get("version_id")
        valid.append(
            Citation(
                citation_id=cited_id,
                entry_key=str(entry["entry_key"]),
                source_id=uuid.UUID(str(entry["source_id"])),
                version_id=uuid.UUID(str(version_id)) if version_id else None,
            )
        )

    return CitationValidationResult(
        valid_citations=tuple(valid),
        bad_citation_ids=tuple(bad),
        missing_required_citation=requires_citation and not valid,
    )


def _citation_to_json(citation: Citation) -> dict[str, object]:
    return {
        "citation_id": citation.citation_id,
        "entry_key": citation.entry_key,
        "source_id": str(citation.source_id),
        "version_id": str(citation.version_id) if citation.version_id is not None else None,
    }


async def apply_citation_validation(
    tenant_id: uuid.UUID, message_id: uuid.UUID, result: CitationValidationResult
) -> None:
    """Persists the validated citation set on ``message.citations`` and any
    flags onto ``message.moderation_flags`` -- ``bad_citation`` (hallucinated ids) and/or
    ``missing_required_citation`` (a ruling-type reply, ``requires_citation`` phase flag
    set, with zero valid citations). Writing an empty ``citations`` list is a legitimate,
    real outcome (a reply that cited nothing), unlike the ``flag_contradictions``,
    which no-ops on nothing to flag -- there's always a citations list to record here,
    even if it's empty."""
    async with tenant_scope(tenant_id) as session:
        message = await session.get(MessageRow, message_id)
        assert message is not None
        message.citations = [_citation_to_json(c) for c in result.valid_citations]

        flags = dict(message.moderation_flags)
        if result.bad_citation_ids:
            flags["bad_citation"] = list(result.bad_citation_ids)
        if result.missing_required_citation:
            flags["missing_required_citation"] = True
        message.moderation_flags = flags


async def resolve_citation(
    tenant_id: uuid.UUID, source_id: uuid.UUID, entry_key: str, version_id: uuid.UUID | None
) -> KnowledgeEntry | None:
    """The pinned-version read: always the entry as it stood when the citation was
    generated, regardless of how many times the source has been published since."""
    return await knowledge_repo.get_entry_by_key_and_version(
        tenant_id, source_id, entry_key, version_id
    )
