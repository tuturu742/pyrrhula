"""Entry-level diff between two versions: a 3-way set (added / removed /
changed) keyed by the stable ``entry_key``, so a diff survives entries being reordered or
the version itself being re-derived — "changed" means the same key now has different
content, not that its row identity changed (published-version entry rows are always brand
new INSERTs per publish, per ``core.knowledge.authoring.publish_version``).

A per-entry content hash isn't a stored column (only the whole-version hash and each
chunk's hash are) — computed here, on the fly, by reusing the same
``core.knowledge.hashing.entry_content_payload`` shape the version hash is built from, so
"changed" means exactly what the version's own content-addressing means: identical
content, differently keyed, always hashes the same either place.

This is an authoring/UI concern (D1.1: "version history + diff view"), not a "stored text
reaching a model" one — lives alongside ``core.knowledge.authoring``, not behind INV-1's
restricted ``core.knowledge.repo``.
"""

from __future__ import annotations

import difflib
import hashlib
import uuid
from dataclasses import dataclass

from core.audit.canonical import canonical_json
from core.knowledge.authoring import list_version_entries
from core.knowledge.hashing import entry_content_payload
from core.knowledge.models import KnowledgeEntry


def _entry_hash(entry: KnowledgeEntry) -> str:
    return hashlib.sha256(canonical_json(entry_content_payload(entry)).encode()).hexdigest()


@dataclass(frozen=True)
class ChangedEntry:
    entry_key: str
    text_diff: str


@dataclass(frozen=True)
class VersionDiff:
    added: list[str]
    removed: list[str]
    changed: list[ChangedEntry]


async def diff_versions(
    tenant_id: uuid.UUID, version_a_id: uuid.UUID, version_b_id: uuid.UUID
) -> VersionDiff:
    """``version_a_id`` is the "before", ``version_b_id`` the "after" — added/removed are
    relative to that direction."""
    entries_a = {e.entry_key: e for e in await list_version_entries(tenant_id, version_a_id)}
    entries_b = {e.entry_key: e for e in await list_version_entries(tenant_id, version_b_id)}

    keys_a, keys_b = set(entries_a), set(entries_b)
    added = sorted(keys_b - keys_a)
    removed = sorted(keys_a - keys_b)

    changed: list[ChangedEntry] = []
    for key in sorted(keys_a & keys_b):
        entry_a, entry_b = entries_a[key], entries_b[key]
        if _entry_hash(entry_a) == _entry_hash(entry_b):
            continue
        text_diff = "\n".join(
            difflib.unified_diff(
                entry_a.body_md.splitlines(),
                entry_b.body_md.splitlines(),
                fromfile=f"{key}@a",
                tofile=f"{key}@b",
                lineterm="",
            )
        )
        changed.append(ChangedEntry(entry_key=key, text_diff=text_diff))

    return VersionDiff(added=added, removed=removed, changed=changed)
