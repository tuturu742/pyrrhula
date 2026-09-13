"""``KnowledgeSourceVersion.content_hash`` (plan §6.1): ``sha256`` of the canonicalised
entry set, reusing T0.7's canonical serializer so this and the audit hash chain can't
quietly drift on what "the hash of a row" means.

Deliberately hashes a fixed, stable subset of ``KnowledgeEntry`` fields, not the whole
ORM row — ``id``/``created_at``/``version_id`` differ across a republish of otherwise
identical content and must not perturb the hash (the acceptance criterion is "content_hash
is reproducible from its entries", i.e. from what an author actually wrote, not from
bookkeeping columns).
"""

from __future__ import annotations

import hashlib
from typing import Any

from core.audit.canonical import canonical_json
from core.knowledge.models import KnowledgeEntry


def entry_content_payload(entry: KnowledgeEntry) -> dict[str, Any]:
    return {
        "entry_key": entry.entry_key,
        "title": entry.title,
        "body_md": entry.body_md,
        "class": entry.class_,
        "scope_key": entry.scope_key,
        "keys": sorted(entry.keys),
        "secondary_keys": sorted(entry.secondary_keys),
        "logic": entry.logic,
        "use_regex": entry.use_regex,
        "constant": entry.constant,
        "sticky": entry.sticky,
        "cooldown": entry.cooldown,
        "delay": entry.delay,
        "trigger_pct": entry.trigger_pct,
        "inclusion_group": entry.inclusion_group,
        "position": entry.position,
        "insertion_order": entry.insertion_order,
    }


def compute_content_hash(entries: list[KnowledgeEntry]) -> str:
    payload = sorted((entry_content_payload(e) for e in entries), key=lambda p: str(p["entry_key"]))
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
