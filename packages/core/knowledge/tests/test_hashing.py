"""Pure unit tests (no DB) for content-hash determinism (plan §6.1 acceptance criterion:
"content_hash is reproducible from its entries")."""

from __future__ import annotations

from core.knowledge.hashing import compute_content_hash
from core.knowledge.models import KnowledgeEntry


def _entry(entry_key: str, body_md: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        entry_key=entry_key,
        title=entry_key,
        body_md=body_md,
        class_="rules",
        scope_key="workspace_public",
        keys=[],
        secondary_keys=[],
        logic="AND",
        use_regex=False,
        constant=False,
        sticky=None,
        cooldown=None,
        delay=None,
        trigger_pct=None,
        inclusion_group=None,
        position="before_char",
        insertion_order=0,
    )


def test_hash_is_reproducible_from_same_entries() -> None:
    entries = [_entry("grappling", "Roll 1d20+STR."), _entry("stealth", "Roll 1d20+DEX.")]
    assert compute_content_hash(entries) == compute_content_hash(entries)


def test_hash_is_order_independent() -> None:
    a = _entry("grappling", "Roll 1d20+STR.")
    b = _entry("stealth", "Roll 1d20+DEX.")
    assert compute_content_hash([a, b]) == compute_content_hash([b, a])


def test_hash_changes_when_body_changes() -> None:
    original = [_entry("grappling", "Roll 1d20+STR.")]
    changed = [_entry("grappling", "Roll 1d20+STR+2.")]
    assert compute_content_hash(original) != compute_content_hash(changed)


def test_hash_ignores_nothing_but_content_fields() -> None:
    """Two entries with identical content but different bookkeeping (id, version_id,
    created_at aren't part of the entry constructor at all here — this is really
    asserting compute_content_hash never reaches for such attributes)."""
    entries = [_entry("grappling", "Roll 1d20+STR.")]
    assert compute_content_hash(entries) == compute_content_hash(
        [_entry("grappling", "Roll 1d20+STR.")]
    )
