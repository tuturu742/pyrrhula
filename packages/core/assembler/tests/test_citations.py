"""C1.8 acceptance criteria for citation validation."""

from __future__ import annotations

import uuid

from core.agents.seed import seed_dev_agent
from core.assembler.citations import (
    apply_citation_validation,
    extract_cited_ids,
    resolve_citation,
    validate_citations,
)
from core.knowledge.authoring import EntryFields, create_source, publish_version, upsert_draft_entry
from core.process.skeleton import create_session, submit_user_message
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _manifest_entry(
    citation_id: str, entry_key: str, source_id: uuid.UUID, version_id: uuid.UUID | None
) -> dict[str, object]:
    return {
        "citation_id": citation_id,
        "entry_id": str(uuid.uuid4()),
        "entry_key": entry_key,
        "source_id": str(source_id),
        "version_id": str(version_id) if version_id else None,
        "class": "rules",
        "bucket": "rules",
        "rank": 1,
        "score": 1.0,
        "why": "dense",
        "token_count": 5,
    }


# ── pure scanner ──────────────────────────────────────────────────────────────────────


def test_extract_cited_ids_finds_all_bracketed_citations() -> None:
    assert extract_cited_ids("As stated in [k1], the rule applies. See also [k3].") == [
        "k1",
        "k3",
    ]


def test_extract_cited_ids_returns_empty_for_no_citations() -> None:
    assert extract_cited_ids("A plain narration with no citations at all.") == []


def test_a_reply_citing_an_id_absent_from_the_manifest_is_flagged() -> None:
    source_id, version_id = uuid.uuid4(), uuid.uuid4()
    manifest_entries = [_manifest_entry("k1", "grappling", source_id, version_id)]

    result = validate_citations(
        "As stated in [k1] and [k9], you succeed.", manifest_entries, requires_citation=False
    )

    assert result.bad_citation_ids == ("k9",)
    assert len(result.valid_citations) == 1
    assert result.valid_citations[0].citation_id == "k1"


def test_a_reply_citing_only_valid_ids_has_no_bad_citations() -> None:
    source_id, version_id = uuid.uuid4(), uuid.uuid4()
    manifest_entries = [_manifest_entry("k1", "grappling", source_id, version_id)]

    result = validate_citations(
        "As stated in [k1], you succeed.", manifest_entries, requires_citation=False
    )
    assert result.bad_citation_ids == ()
    assert result.missing_required_citation is False


def test_duplicate_bad_citation_ids_are_deduplicated() -> None:
    result = validate_citations("[k9] and again [k9]", [], requires_citation=False)
    assert result.bad_citation_ids == ("k9",)


def test_requires_citation_phase_with_zero_citations_is_flagged() -> None:
    result = validate_citations("A ruling with no citation at all.", [], requires_citation=True)
    assert result.missing_required_citation is True


def test_requires_citation_phase_with_a_valid_citation_is_not_flagged() -> None:
    source_id, version_id = uuid.uuid4(), uuid.uuid4()
    manifest_entries = [_manifest_entry("k1", "grappling", source_id, version_id)]
    result = validate_citations("Per [k1], you succeed.", manifest_entries, requires_citation=True)
    assert result.missing_required_citation is False


def test_requires_citation_false_never_flags_missing_citation() -> None:
    result = validate_citations("No citations here.", [], requires_citation=False)
    assert result.missing_required_citation is False


# ── persistence + pinned-version resolution, against a live Postgres ───────────────


async def test_apply_citation_validation_writes_citations_and_bad_citation_flag(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"citations-apply-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    message = await submit_user_message(tenant_id, sess.id, uuid.uuid4(), "irrelevant")

    source_id, version_id = uuid.uuid4(), uuid.uuid4()
    manifest_entries = [_manifest_entry("k1", "grappling", source_id, version_id)]
    result = validate_citations(
        "Per [k1] and the fabricated [k9], you succeed.", manifest_entries, requires_citation=False
    )

    await apply_citation_validation(tenant_id, message.id, result)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(MessageRow, message.id)
        assert row is not None
        assert len(row.citations) == 1
        assert row.citations[0]["citation_id"] == "k1"
        assert row.moderation_flags["bad_citation"] == ["k9"]


async def test_citation_resolution_returns_the_pinned_versions_text_after_two_more_publishes(
    db_available: None,
) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"citations-pin-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")

    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling", body_md="V1 text", class_="rules", scope_key="workspace_public"
        ),
    )
    version_1 = await publish_version(tenant_id, source.id, change_note="v1")

    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling", body_md="V2 text", class_="rules", scope_key="workspace_public"
        ),
    )
    await publish_version(tenant_id, source.id, change_note="v2")

    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling", body_md="V3 text", class_="rules", scope_key="workspace_public"
        ),
    )
    await publish_version(tenant_id, source.id, change_note="v3")

    # A citation generated back when version_1 was current still resolves to V1's text,
    # even though the source has been published twice more since.
    resolved = await resolve_citation(tenant_id, source.id, "grappling", version_1.id)
    assert resolved is not None
    assert resolved.body_md == "V1 text"


async def test_citation_resolution_returns_none_for_a_nonexistent_entry(
    db_available: None,
) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"citations-missing-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    resolved = await resolve_citation(tenant_id, source.id, "nonexistent", uuid.uuid4())
    assert resolved is None
