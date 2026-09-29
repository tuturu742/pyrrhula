"""A bucket full of always-on entries retrieves nothing, and the product can say so.

The measured case this exists for: nine constant rules entries (~1,100 tokens) against a
resolve phase that budgets rules ~1,050. A second rules source was attached, published,
embedded and searched, and contributed nothing to thirty consecutive turns.
"""

from __future__ import annotations

import uuid

from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.retrieval.saturation import workspace_saturation
from core.process.dsl.schema import (
    ActorSpec,
    BudgetSpec,
    PhaseSpec,
    ProcessDefinitionDSL,
    VisibilitySpec,
)
from core.tenancy.seed import seed_dev_tenant


def _phase(ratio: dict[str, float], max_tokens: int, *, history_ratio: float = 0.0) -> PhaseSpec:
    return PhaseSpec(
        label_key="resolve",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=list(ratio),
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio=ratio, max_tokens=max_tokens, history_ratio=history_ratio),
    )


def _dsl(**phases: PhaseSpec) -> ProcessDefinitionDSL:
    return ProcessDefinitionDSL(
        name="test flow",
        vocabulary_overlay="default_v1",
        initial_phase=next(iter(phases)),
        phases=dict(phases),
    )


async def _seed(slug: str, entries: list[tuple[str, str, bool]]) -> tuple[uuid.UUID, uuid.UUID]:
    """``entries`` is (entry_key, body, constant); every entry is a rules entry."""
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    source = await create_source(tenant_id, key="rules", name="Rules", class_="rules")
    for entry_key, body, constant in entries:
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry_key,
            EntryFields(
                title=entry_key,
                body_md=body,
                class_="rules",
                scope_key="workspace_public",
                constant=constant,
            ),
        )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, "workspace_public", version_pin=version.id
    )
    return tenant_id, workspace_id


async def test_constants_beyond_the_share_are_reported_as_not_fitting(
    db_available: None,
) -> None:
    """Three always-on entries larger than the share against a small bucket: the first is
    admitted whatever its size, the rest do not fit, and retrieval keeps what is left."""
    body = " ".join(f"word{i}" for i in range(60))
    tenant_id, workspace_id = await _seed(
        "sat-full", [(f"always-{i}", body, True) for i in range(3)]
    )

    report = await workspace_saturation(
        tenant_id, workspace_id, _dsl(resolve=_phase({"rules": 1.0}, 200))
    )

    assert len(report) == 1
    rules = report[0].classes[0]
    assert rules.class_ == "rules"
    assert rules.bucket_tokens == 200
    assert rules.constant_entries == 3
    assert rules.constant_tokens > rules.bucket_tokens
    # One admitted, two left out -- and the bucket is not saturated, because the cap
    # kept room that retrieval can still spend.
    assert rules.admitted_constant_entries == 1
    assert rules.dropped_constant_entries == 2
    assert rules.retrievable_tokens > 0
    assert not rules.saturated
    # Offered in the author's order, so "which one got in" is a decision they made.
    assert rules.constant_entry_keys == ["always-0", "always-1", "always-2"]


async def test_one_constant_larger_than_the_whole_bucket_saturates_it(
    db_available: None,
) -> None:
    """The only way left to saturate a class: a single always-on entry bigger than the
    share it sits in. It is admitted anyway -- an always-on entry that never appears is
    not one -- and it leaves nothing behind."""
    body = " ".join(f"word{i}" for i in range(120))
    tenant_id, workspace_id = await _seed("sat-one", [("always", body, True)])

    # Ask once with room to spare to learn what the entry actually occupies, then give
    # the class exactly that -- rather than hard-coding a number the chunker owns.
    roomy = await workspace_saturation(
        tenant_id, workspace_id, _dsl(resolve=_phase({"rules": 1.0}, 100_000))
    )
    exactly_full = roomy[0].classes[0].constant_tokens

    report = await workspace_saturation(
        tenant_id, workspace_id, _dsl(resolve=_phase({"rules": 1.0}, exactly_full))
    )

    rules = report[0].classes[0]
    assert rules.admitted_constant_entries == 1
    assert rules.dropped_constant_entries == 0
    assert rules.saturated
    assert rules.retrievable_tokens == 0


async def test_a_bucket_with_room_is_not_saturated(db_available: None) -> None:
    body = " ".join(f"word{i}" for i in range(20))
    tenant_id, workspace_id = await _seed("sat-room", [("always", body, True)])

    report = await workspace_saturation(
        tenant_id, workspace_id, _dsl(resolve=_phase({"rules": 1.0}, 4000))
    )

    rules = report[0].classes[0]
    assert not rules.saturated
    assert not rules.tight
    assert rules.admitted_constant_entries == 1
    assert rules.dropped_constant_entries == 0
    assert rules.retrievable_tokens > 3000


async def test_non_constant_entries_do_not_occupy_the_bucket(db_available: None) -> None:
    """A big source is not the problem: only always-on entries take room before
    retrieval, which is the whole finding this module encodes."""
    body = " ".join(f"word{i}" for i in range(400))
    tenant_id, workspace_id = await _seed(
        "sat-big", [(f"entry-{i}", body, False) for i in range(5)]
    )

    report = await workspace_saturation(
        tenant_id, workspace_id, _dsl(resolve=_phase({"rules": 1.0}, 500))
    )

    rules = report[0].classes[0]
    assert rules.constant_tokens == 0
    assert rules.constant_entries == 0
    assert not rules.saturated
    assert rules.retrievable_tokens == 500


async def test_the_history_reservation_and_ratio_shrink_the_bucket(db_available: None) -> None:
    """The bucket is what retrieval actually gets: max_tokens, minus the history
    reservation, times the class's share."""
    body = " ".join(f"word{i}" for i in range(50))
    tenant_id, workspace_id = await _seed("sat-ratio", [("always", body, True)])

    report = await workspace_saturation(
        tenant_id,
        workspace_id,
        _dsl(resolve=_phase({"rules": 0.7, "lore": 0.3}, 1000, history_ratio=0.3)),
    )

    by_class = {c.class_: c for c in report[0].classes}
    assert report[0].history_reserved_tokens == 300
    # (1000 - 300) * 0.7, truncated -- split_budget floors, and 0.7 is not exact in
    # binary, so the arithmetic that runs is the arithmetic asserted.
    assert by_class["rules"].bucket_tokens == 489
    assert by_class["lore"].bucket_tokens == 210
    # The lore bucket has no constants of its own; only rules is loaded.
    assert by_class["lore"].constant_tokens == 0
    assert by_class["rules"].constant_tokens > 0


async def test_a_phase_out_of_scope_for_the_constants_counts_none(db_available: None) -> None:
    """Constants are counted over the scopes the phase declares. A phase that does not
    declare the entries' scope cannot receive them, so they load nothing."""
    body = " ".join(f"word{i}" for i in range(60))
    tenant_id, workspace_id = await _seed("sat-scope", [("always", body, True)])

    phase = _phase({"rules": 1.0}, 100)
    phase = phase.model_copy(
        update={"visibility": phase.visibility.model_copy(update={"scopes": ["referee_only"]})}
    )
    report = await workspace_saturation(tenant_id, workspace_id, _dsl(resolve=phase))

    assert report[0].classes[0].constant_tokens == 0
    assert not report[0].classes[0].saturated


async def test_phases_without_a_budget_are_skipped(db_available: None) -> None:
    tenant_id, workspace_id = await _seed("sat-nobudget", [("always", "short", True)])
    awaiting = PhaseSpec(
        label_key="await",
        actors=[],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=[], entity_fields=[], secrets="none"
        ),
    )
    report = await workspace_saturation(
        tenant_id, workspace_id, _dsl(resolve=_phase({"rules": 1.0}, 500), waiting=awaiting)
    )
    assert [p.phase_key for p in report] == ["resolve"]
