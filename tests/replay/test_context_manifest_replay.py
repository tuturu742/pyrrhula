"""INV-10, CI-blocking: any turn's rendered context must be reproducible from the
same recorded inputs. ``context_manifest`` doesn't store query text/embeddings (the plan's
own schema has no such column -- only the *result* of assembly: entries, redactions,
token_counts, and the hash of what was rendered), so replay here means the property
INV-10 actually needs: re-running ``assemble()`` with the same inputs that produced a
persisted manifest reproduces ``rendered_hash`` exactly, proven turn-by-turn across a full
20-turn scripted session with real sticky/cooldown keyword-activation state evolving
between turns -- not just a single static call, since determinism has to survive state
that changes every turn, which is where a hidden nondeterminism (unstable dict ordering,
wall-clock, an unseeded RNG) would actually surface.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import assemble
from core.assembler.manifest import write_context_manifest
from core.knowledge.activation import EntryActivationState, activate_entries
from core.knowledge.authoring import create_source
from core.knowledge.retrieval.tests.conftest import unit_vector
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass(frozen=True)
class _FakeEntry:
    """Matches ``core.knowledge.activation.ActivatableEntry``'s Protocol shape -- the
    activation computation itself needs no DB round-trip; only the chunks it activates do."""

    id: uuid.UUID
    entry_key: str
    keys: list[str]
    secondary_keys: list[str] = field(default_factory=list)
    logic: str = "AND"
    use_regex: bool = False
    constant: bool = False
    sticky: int | None = None
    cooldown: int | None = None
    delay: int | None = None
    trigger_pct: int | None = None
    inclusion_group: str | None = None
    insertion_order: int = 0


async def _seed_entry_with_chunk(
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    entry_id: uuid.UUID,
    *,
    entry_key: str,
    body_text: str,
    scope_key: str,
    embedding: list[float],
) -> None:
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO knowledge_entry "
                "(id, tenant_id, knowledge_source_id, version_id, entry_key, title, "
                " body_md, class, scope_key, keys, secondary_keys, logic, "
                " use_regex, constant, position, insertion_order) "
                "VALUES (:id, :tenant_id, :source_id, NULL, :entry_key, :entry_key, "
                " :body_text, 'rules', :scope_key, '{}', '{}', 'AND', false, "
                " false, 'before_char', 0)"
            ),
            {
                "id": entry_id,
                "tenant_id": tenant_id,
                "source_id": source_id,
                "entry_key": entry_key,
                "body_text": body_text,
                "scope_key": scope_key,
            },
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, "
                " class, scope_key, embedding, content_hash) "
                "VALUES (:tenant_id, :entry_id, NULL, 0, :text, :token_count, "
                " 'rules', :scope_key, CAST(:vec AS vector), :content_hash)"
            ),
            {
                "tenant_id": tenant_id,
                "entry_id": entry_id,
                "text": body_text,
                "token_count": len(body_text.split()),
                "scope_key": scope_key,
                "vec": "[" + ",".join(repr(float(x)) for x in embedding) + "]",
                "content_hash": uuid.uuid4().hex,
            },
        )


async def test_replay_across_a_scripted_20_turn_session_with_sticky_cooldown_state(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"replay-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")

    async with tenant_scope(tenant_id) as session:
        viewer = Principal(tenant_id=tenant_id, kind="human", display_name="viewer")
        session.add(viewer)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=viewer.id,
                role="participant",
            )
        )
        viewer_id = viewer.id

    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    session_id = sess.id

    # 'sticky-rule' fires on "grapple" and then stays active for 3 turns (sticky=3);
    # 'cooldown-rule' fires on "fire" and then can't refire for 4 turns (cooldown=4) --
    # real, evolving sticky/cooldown state across the whole 20-turn script.
    sticky_id = uuid.uuid4()
    cooldown_id = uuid.uuid4()
    await _seed_entry_with_chunk(
        tenant_id,
        source.id,
        sticky_id,
        entry_key="sticky-rule",
        body_text="Grappling stays contested until someone breaks free.",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await _seed_entry_with_chunk(
        tenant_id,
        source.id,
        cooldown_id,
        entry_key="cooldown-rule",
        body_text="Fire damage burns for one additional round.",
        scope_key="workspace_public",
        embedding=unit_vector(1),
    )

    fake_entries = [
        _FakeEntry(id=sticky_id, entry_key="sticky-rule", keys=["grapple"], sticky=3),
        _FakeEntry(id=cooldown_id, entry_key="cooldown-rule", keys=["fire"], cooldown=4),
    ]

    phase = PhaseSpec(
        label_key="turn",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=["rules"],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={"rules": 1.0}, max_tokens=500),
    )

    # A 20-turn script: "grapple" on turns 0 and 10 (re-triggering sticky), "fire" on
    # turn 2 (then blocked by cooldown until turn 6), plain turns otherwise.
    scan_texts = ["plain turn"] * 20
    scan_texts[0] = "I try to grapple the guard"
    scan_texts[2] = "I cast a fire spell"
    scan_texts[6] = "I cast a fire spell again"  # cooldown lifted by now (until_turn=6)
    scan_texts[10] = "I grapple again"

    prior_state: dict[str, EntryActivationState] = {}
    rng_seed = f"replay-{session_id}"

    for turn_index in range(20):
        activation = activate_entries(
            fake_entries,
            scan_text=scan_texts[turn_index],
            turn_index=turn_index,
            prior_state=prior_state,
            rng_seed=rng_seed,
        )
        prior_state = activation.new_state
        activated_by_class = {"rules": activation.activated} if activation.activated else None

        assemble_kwargs = dict(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            session_id=session_id,
            query_text=scan_texts[turn_index],
            query_embedding=unit_vector(0),
            history_max_tokens=0,
            activated_entries_by_class=activated_by_class,
        )

        original = await assemble(viewer, phase, **assemble_kwargs)  # type: ignore[arg-type]
        manifest_row = await write_context_manifest(
            tenant_id, session_id, turn_index, viewer_id, phase.label_key, original
        )
        assert manifest_row.rendered_hash == original.content_hash

        # Replay: re-run assemble() with the exact same recorded inputs (the corpus is
        # append-only/unchanged between the two calls) and confirm the hash still matches
        # what was persisted -- the INV-10 property itself.
        replayed = await assemble(viewer, phase, **assemble_kwargs)  # type: ignore[arg-type]
        assert replayed.content_hash == manifest_row.rendered_hash, (
            f"replay diverged at turn {turn_index}"
        )

    # Sanity: sticky/cooldown genuinely changed which entries were active across turns --
    # this wasn't a script where nothing ever activated.
    assert any(s.sticky_until_turn is not None for s in prior_state.values())
    assert any(s.cooldown_until_turn is not None for s in prior_state.values())
