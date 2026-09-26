"""Acceptance criteria for the ContextAssembler, against a live Postgres."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import (
    ContextManifest,
    EntityStateBlock,
    Redaction,
    assemble,
)
from core.knowledge.activation import ActivatedEntry
from core.knowledge.authoring import create_source
from core.knowledge.retrieval.tests.conftest import (
    attach_to_workspace,
    seed_chunk,
    unit_vector,
)
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session, submit_user_message
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _phase(scopes: list[str], ratio: dict[str, float], max_tokens: int) -> PhaseSpec:
    return PhaseSpec(
        label_key="test_phase",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=list(ratio), scopes=scopes, entity_fields="all", secrets="none"
        ),
        budget=BudgetSpec(ratio=ratio, max_tokens=max_tokens),
    )


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, Principal]:
    """Returns (tenant_id, workspace_id, source_id, viewer) -- viewer is a Principal with
    a 'participant' WorkspaceMembership, so it's entitled to 'workspace_public' but not
    'facilitator_only'."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    # A source a workspace has not attached is not that workspace's knowledge: the
    # assembler resolves which version to read from the attachment, so an unattached
    # source retrieves nothing. Seeded and imported tenants always attach; the fixture
    # only ever skipped it because retrieval used to ignore versions entirely.
    await attach_to_workspace(tenant_id, workspace_id, source.id)

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

    return tenant_id, workspace_id, source.id, viewer


async def _session_for(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, viewer: Principal
) -> uuid.UUID:
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return sess.id


async def _seed_constant_entry(
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    entry_id: uuid.UUID,
    *,
    entry_key: str,
    body_text: str,
) -> None:
    """A ``constant: true`` entry -- always active regardless of query (the stable
    knowledge). ``seed_chunk`` (the test helper) hardcodes ``constant=false``, so this
    is a local, minimal insert for exactly the one field it doesn't expose."""
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO knowledge_entry "
                "(id, tenant_id, knowledge_source_id, version_id, entry_key, title, "
                " body_md, class, scope_key, keys, secondary_keys, logic, "
                " use_regex, constant, position, insertion_order) "
                "VALUES (:id, :tenant_id, :source_id, NULL, :entry_key, :entry_key, "
                " :body_text, 'rules', 'workspace_public', '{}', '{}', 'AND', false, "
                " true, 'before_char', 0)"
            ),
            {
                "id": entry_id,
                "tenant_id": tenant_id,
                "source_id": source_id,
                "entry_key": entry_key,
                "body_text": body_text,
            },
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, "
                " class, scope_key, embedding, content_hash) "
                "VALUES (:tenant_id, :entry_id, NULL, 0, :text, :token_count, "
                " 'rules', 'workspace_public', NULL, :content_hash)"
            ),
            {
                "tenant_id": tenant_id,
                "entry_id": entry_id,
                "text": body_text,
                "token_count": len(body_text.split()),
                "content_hash": uuid.uuid4().hex,
            },
        )


# ── INV-2: required, defaultless viewer/phase ───────────────────────────────────────


async def test_assemble_without_viewer_or_phase_does_not_typecheck() -> None:
    """Static proof: assemble's signature has no default and no Optional on either
    parameter, so omitting either is a mypy error at any real call site -- this test
    documents/pins that by inspecting the signature directly rather than compiling a
    deliberately-broken call site into the suite."""
    import inspect

    sig = inspect.signature(assemble)
    assert sig.parameters["viewer"].default is inspect.Parameter.empty
    assert sig.parameters["phase"].default is inspect.Parameter.empty


async def test_assemble_runtime_guard_raises_on_none(db_available: None) -> None:
    tenant_id, workspace_id, _source_id, viewer = await _setup("ctx-guard")
    session_id = await _session_for(tenant_id, workspace_id, viewer)
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    with pytest.raises(TypeError):
        await assemble(
            None,  # type: ignore[arg-type]
            phase,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            session_id=session_id,
            query_text="q",
            query_embedding=unit_vector(0),
            history_max_tokens=100,
        )

    with pytest.raises(TypeError):
        await assemble(
            viewer,
            None,  # type: ignore[arg-type]
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            session_id=session_id,
            query_text="q",
            query_embedding=unit_vector(0),
            history_max_tokens=100,
        )


# ── determinism ──────────────────────────────────────────────────────────────────────


async def test_assemble_is_deterministic_across_repeated_calls(db_available: None) -> None:
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-determinism")
    session_id = await _session_for(tenant_id, workspace_id, viewer)
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="grappling",
        body_text="Roll 1d20 plus strength to grapple the target.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    first = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="grapple check",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
    )
    second = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="grapple check",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
    )

    assert first.rendered_context == second.rendered_context
    assert first.content_hash == second.content_hash
    assert first.content_hash == first.content_hash.lower()  # sanity: it's a real hex digest
    assert len(first.content_hash) == 64  # sha256 hex length


# ── out-of-scope leak prevention (adversarial) ──────────────────────────────────────


async def test_out_of_scope_leak_bait_never_appears_even_as_best_match(
    db_available: None,
) -> None:
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-leak")
    session_id = await _session_for(tenant_id, workspace_id, viewer)

    # The leak-bait: exact embedding match, foreign scope the viewer has no relationship to.
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="thieves-secret-plan",
        body_text="THE THIEVES GUILD SECRET RENDEZVOUS IS AT MIDNIGHT.",
        class_="rules",
        scope_key="faction_thieves",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="public-rule",
        body_text="Public grappling rule text.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(5),  # orthogonal, objectively worse match
    )
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="secret rendezvous",
        query_embedding=unit_vector(0),  # identical to the out-of-scope chunk's vector
        history_max_tokens=100,
    )

    assert "THIEVES" not in manifest.rendered_context
    assert "RENDEZVOUS" not in manifest.rendered_context
    assert all(e.entry_key != "thieves-secret-plan" for e in manifest.entries)
    assert any(e.entry_key == "public-rule" for e in manifest.entries)


async def test_viewer_with_no_workspace_relationship_gets_no_knowledge(
    db_available: None,
) -> None:
    tenant_id, workspace_id, source_id, _viewer = await _setup("ctx-outsider")
    async with tenant_scope(tenant_id) as session:
        outsider = Principal(tenant_id=tenant_id, kind="human", display_name="outsider")
        session.add(outsider)
        await session.flush()

    session_id = await _session_for(tenant_id, workspace_id, outsider)
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="public-rule",
        body_text="Public rule text.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    manifest = await assemble(
        outsider,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="rule",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
    )

    assert manifest.entries == ()
    assert "Public rule text" not in manifest.rendered_context


# ── budget property ──────────────────────────────────────────────────────────────────


async def test_rendered_knowledge_respects_bucket_budget_within_one_chunk(
    db_available: None,
) -> None:
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-budget")
    session_id = await _session_for(tenant_id, workspace_id, viewer)
    # 5 rules chunks, each ~10 tokens, all matching the query -- budget only fits 3.
    for i in range(5):
        await seed_chunk(
            tenant_id,
            source_id,
            entry_key=f"rule-{i}",
            body_text=" ".join([f"word{i}"] * 10),
            class_="rules",
            scope_key="workspace_public",
            embedding=unit_vector(0),
        )
    phase = _phase(["workspace_public"], {"rules": 1.0}, 35)  # ~3 chunks worth

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="word0",
        query_embedding=unit_vector(0),
        history_max_tokens=0,
    )

    assert manifest.token_counts["rules"] <= 35 + 10  # bucket ± one chunk (~10 tok/chunk)
    assert len(manifest.entries) < 5  # budget genuinely excluded some candidates


async def test_phase_with_no_budget_produces_no_knowledge_but_does_not_crash(
    db_available: None,
) -> None:
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-no-budget")
    session_id = await _session_for(tenant_id, workspace_id, viewer)
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule",
        body_text="Some rule text.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    phase = PhaseSpec(
        label_key="await_only",
        actors=[ActorSpec(human_participant="all", mode="free")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=["workspace_public"], entity_fields="all", secrets="none"
        ),
        budget=None,
    )

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="rule",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
    )

    assert manifest.entries == ()
    assert "rules" not in manifest.token_counts


# ── citation envelope ────────────────────────────────────────────────────────────────


async def test_included_chunks_get_a_citation_envelope(db_available: None) -> None:
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-citation")
    session_id = await _session_for(tenant_id, workspace_id, viewer)
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="grappling",
        body_text="Roll 1d20 plus strength to grapple.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="grapple",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
    )

    assert len(manifest.entries) == 1
    entry = manifest.entries[0]
    assert entry.citation_id == "k1"
    assert '<knowledge id="k1" class="rules" source="Core Rules" entry="grappling">' in (
        manifest.rendered_context
    )
    assert "Roll 1d20 plus strength to grapple." in manifest.rendered_context
    assert "</knowledge>" in manifest.rendered_context


# ── history window ───────────────────────────────────────────────────────────────────


async def test_history_window_includes_recent_turns_under_its_own_budget(
    db_available: None,
) -> None:
    tenant_id, workspace_id, _source_id, viewer = await _setup("ctx-history")
    session_id = await _session_for(tenant_id, workspace_id, viewer)

    for i in range(5):
        await submit_user_message(tenant_id, session_id, viewer.id, f"message number {i}")

    phase = _phase(["workspace_public"], {"rules": 1.0}, 0)

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="",
        query_embedding=unit_vector(0),
        history_max_tokens=1000,
    )

    for i in range(5):
        assert f"message number {i}" in manifest.rendered_context

    # A tight history budget keeps only the most recent turns, not the oldest.
    tight_manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="",
        query_embedding=unit_vector(0),
        history_max_tokens=9,  # "user: message number N" = 4 words; fits ~2 turns
    )
    assert "message number 4" in tight_manifest.rendered_context
    assert "message number 0" not in tight_manifest.rendered_context


# ── injection seams (entity state / secrets gate) actually get called ───────────────


async def test_entity_state_and_secrets_gate_seams_flow_into_the_manifest(
    db_available: None,
) -> None:
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-seams")
    session_id = await _session_for(tenant_id, workspace_id, viewer)
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule",
        body_text="Rule text mentioning a hidden fact.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    async def fake_entity_renderer(tenant_id, workspace_id, session_id, viewer, phase):  # noqa: ANN001
        return EntityStateBlock(rendered_text="Entity: the door is locked.", token_count=5)

    async def fake_secrets_gate(rendered_knowledge, viewer, phase, session_id):  # noqa: ANN001
        # The assembler calls this once per layout side (stable/volatile); only actually redact
        # (and report a Redaction) on the side that contains the match, matching how a
        # real gate would behave -- an unconditional redaction here would double-count.
        if "hidden fact" not in rendered_knowledge:
            return rendered_knowledge, ()
        redacted = rendered_knowledge.replace("hidden fact", "[REDACTED]")
        return redacted, (Redaction(type="secret", id="s1", reason="undisclosed"),)

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="rule",
        query_embedding=unit_vector(0),
        history_max_tokens=0,
        entity_state_renderer=fake_entity_renderer,
        secrets_gate=fake_secrets_gate,
    )

    assert "Entity: the door is locked." in manifest.rendered_context
    assert "[REDACTED]" in manifest.rendered_context
    assert "hidden fact" not in manifest.rendered_context
    assert manifest.redactions == (Redaction(type="secret", id="s1", reason="undisclosed"),)
    assert manifest.token_counts["entity_state"] == 5


async def test_default_seams_are_true_no_ops(db_available: None) -> None:
    tenant_id, workspace_id, _source_id, viewer = await _setup("ctx-noop-seams")
    session_id = await _session_for(tenant_id, workspace_id, viewer)
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="",
        query_embedding=unit_vector(0),
        history_max_tokens=0,
    )

    assert isinstance(manifest, ContextManifest)
    assert manifest.redactions == ()
    assert manifest.token_counts["entity_state"] == 0


# ── stable/volatile layout ─────────────────────────────────────────────────────


async def test_two_consecutive_turns_share_a_byte_identical_stable_prefix(
    db_available: None,
) -> None:
    """The literal acceptance criterion: entity state + constant knowledge don't depend
    on query/history, so the stable prefix must be identical turn to turn even though the
    volatile suffix (retrieved chunks + history) genuinely differs."""
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-stable-prefix")
    session_id = await _session_for(tenant_id, workspace_id, viewer)

    constant_id = uuid.uuid4()
    await _seed_constant_entry(
        tenant_id,
        source_id,
        constant_id,
        entry_key="always-on-rule",
        body_text="This rule is always in context.",
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="turn-specific-a",
        body_text="Content relevant to the first turn's query.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="turn-specific-b",
        body_text="Content relevant to the second turn's query.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(3),
    )

    activated = {
        "rules": [
            ActivatedEntry(entry_id=constant_id, entry_key="always-on-rule", rank=1, why="constant")
        ]
    }
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    turn_one = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="first turn query",
        query_embedding=unit_vector(0),
        history_max_tokens=0,
        activated_entries_by_class=activated,
    )
    await submit_user_message(tenant_id, session_id, viewer.id, "the first turn happened")

    turn_two = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="second turn query",
        query_embedding=unit_vector(3),
        history_max_tokens=100,  # now there's history too, unlike turn one
        activated_entries_by_class=activated,
    )

    assert turn_one.stable_prefix == turn_two.stable_prefix
    assert "This rule is always in context." in turn_one.stable_prefix
    assert turn_one.volatile_suffix != turn_two.volatile_suffix
    assert turn_one.rendered_context != turn_two.rendered_context


async def test_constant_entries_render_in_the_stable_prefix_not_the_volatile_suffix(
    db_available: None,
) -> None:
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-constant-split")
    session_id = await _session_for(tenant_id, workspace_id, viewer)

    constant_id = uuid.uuid4()
    await _seed_constant_entry(
        tenant_id, source_id, constant_id, entry_key="constant-rule", body_text="Always here."
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="retrieved-rule",
        body_text="Found by the query.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    activated = {
        "rules": [
            ActivatedEntry(entry_id=constant_id, entry_key="constant-rule", rank=1, why="constant")
        ]
    }
    phase = _phase(["workspace_public"], {"rules": 1.0}, 1000)

    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="query",
        query_embedding=unit_vector(0),
        history_max_tokens=0,
        activated_entries_by_class=activated,
    )

    assert "Always here." in manifest.stable_prefix
    assert "Found by the query." not in manifest.stable_prefix
    assert "Found by the query." in manifest.volatile_suffix
    assert "Always here." not in manifest.volatile_suffix


def test_stable_prefix_plus_volatile_suffix_reconstructs_rendered_context() -> None:
    """Sanity on the invariant every consumer of the manifest relies on."""
    # A live-DB call already exercises this in the tests above; this is a pure structural
    # check against LayoutSections directly, matching test_layout.py's own unit style.
    from core.assembler.layout import LayoutSections

    sections = LayoutSections(stable=("stable stuff",), volatile=("volatile stuff",))
    assert sections.stable_text + "\n\n" + sections.volatile_text == sections.render()


# ── who spoke: history is a transcript of a table, not one voice ────────────────────


async def test_history_names_each_speaker_rather_than_their_role(db_available: None) -> None:
    """Every agent turn used to render as ``assistant``, which tells a model at a
    six-seat table that there is one seat. The observed cost was not cosmetic: a referee
    wrote a player's whole action and narrated its result, and a player answered in the
    referee's voice -- both of which look like a model behaving badly and were the
    transcript refusing to say who spoke."""
    tenant_id, workspace_id, source_id, viewer = await _setup("ctx-speakers")
    session_id = await _session_for(tenant_id, workspace_id, viewer)

    async with tenant_scope(tenant_id) as session:
        other = Principal(tenant_id=tenant_id, kind="human", display_name="Grace")
        session.add(other)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=other.id,
                role="participant",
            )
        )
        other_id = other.id

    await submit_user_message(tenant_id, session_id, viewer.id, "the door was already open")
    await submit_user_message(tenant_id, session_id, other_id, "then somebody opened it")

    manifest = await assemble(
        viewer,
        _phase(["workspace_public"], {"rules": 1.0}, 0),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="",
        query_embedding=unit_vector(0),
        history_max_tokens=1000,
    )

    assert "viewer: the door was already open" in manifest.rendered_context
    assert "Grace: then somebody opened it" in manifest.rendered_context


async def test_a_persona_is_named_by_its_persona_name(db_available: None) -> None:
    """A persona and the principal behind it can carry different names, and the one the
    table uses -- the one the UI shows against the turn -- is the persona's."""
    from core.agents.models import Persona as PersonaRow

    tenant_id, workspace_id, _source_id, viewer = await _setup("ctx-persona-name")
    persona_id = await seed_dev_agent(tenant_id, workspace_id, key="referee", name="The Referee")
    async with tenant_scope(tenant_id) as session:
        persona = await session.get(PersonaRow, persona_id)
        principal_id = persona.principal_id
        # The principal keeps the name it was created with; the persona is renamed the
        # way a workspace renames a seat.
        persona.name = "Kriminalinspektör Lind"
        await session.flush()

    session_id = await _session_for(tenant_id, workspace_id, viewer)
    await submit_user_message(tenant_id, session_id, principal_id, "nobody leaves this room")

    manifest = await assemble(
        viewer,
        _phase(["workspace_public"], {"rules": 1.0}, 0),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="",
        query_embedding=unit_vector(0),
        history_max_tokens=1000,
    )

    assert "Kriminalinspektör Lind: nobody leaves this room" in manifest.rendered_context
    assert "assistant: nobody leaves this room" not in manifest.rendered_context
