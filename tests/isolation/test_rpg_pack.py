"""its own acceptance tests for the RPG pack, loaded through the generic
``core.packs.loader`` -- no RPG-specific code anywhere outside ``packs/rpg/`` itself.
"""

from __future__ import annotations

import pathlib
import uuid

from sqlalchemy import select

from core.agents.seed import seed_dev_agent
from core.entities.repo import get_schema
from core.entities.storage import create_entity
from core.entities.tags import widget_for
from core.entities.validation import compute_derived
from core.knowledge.authoring import EntryFields, get_source
from core.knowledge.library import LIBRARY_TENANT_ID, fork_if_library, seed_library_source
from core.packs.loader import load_pack
from core.process.skeleton import create_session
from core.resolution.rule_system import RuleSystemDefinition
from core.resolution.service import resolve
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.vocabulary.service import get_overlay_by_key

_PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / ".plugins" / "default" / "rpg"
# The rpg pack ships no seed: a specific ruleset's reference text travels with the game
# that uses it, not with the generic workflow. swdev's handbook is the seeded one.
_SEEDED_PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / ".plugins" / "default" / "swdev"


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def test_create_character_yields_populated_sheet_from_pack_templates(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """ "Create character" from the pack template yields a populated, valid entity
    whose tagged fields resolve through the generic tag->widget registry -- no field
    builder, no empty schema (F3.10 builds the actual React rendering; this proves the
    backend half: the pack template alone is enough to produce a real character)."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    loaded = await load_pack(_PACK_DIR, tenant_a, workspace_id)
    assert "character" in loaded.schema_ids

    schema_row = await get_schema(tenant_a, loaded.schema_ids["character"])
    assert schema_row is not None
    definition = schema_row.to_definition()

    character_data = {
        "strength": 16,
        "dexterity": 14,
        "constitution": 15,
        "intelligence": 10,
        "wisdom": 12,
        "charisma": 8,
        "max_hit_points": 20,
        "hit_points": 20,
        "conditions": [],
        "xp": 350,
    }
    entity = await create_entity(
        tenant_a,
        workspace_id,
        schema_row.id,
        definition,
        key=f"hero-{uuid.uuid4().hex[:8]}",
        name="A New Hero",
        scope_key="workspace_public",
        data=character_data,
    )

    # Populated, not a blank field list.
    assert entity.data["strength"] == 16
    derived = compute_derived(definition, entity.data)
    assert derived["level"] == 1  # xp=350 -> 350/1000+1 == 1

    # Every tagged field resolves through the generic registry -- no domain-conditional
    # widget lookup exists anywhere; the pack's own tags are the only thing that varies.
    strength_field = next(f for f in definition.fields if f.key == "strength")
    hit_points_field = next(f for f in definition.fields if f.key == "hit_points")
    conditions_field = next(f for f in definition.fields if f.key == "conditions")
    assert widget_for(strength_field.tags[0]) == "attribute_block"
    assert widget_for(hit_points_field.tags[0]) == "resource_bar"
    assert widget_for(conditions_field.tags[0]) == "status_chip_row"

    overlay = await get_overlay_by_key(tenant_a, "rpg_v1")
    assert overlay is not None
    assert overlay.labels["group.attributes"] == "Attributes"  # pack-owned label present


async def test_three_resolution_shapes_run_through_unchanged_core(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    await load_pack(_PACK_DIR, tenant_a, workspace_id)

    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    sess = await create_session(tenant_a, workspace_id, persona_id)

    async def _rule_system(key: str) -> tuple[RuleSystemDefinition, uuid.UUID]:
        from core.resolution.rule_system import get_rule_system

        row = await get_rule_system(tenant_a, key)
        assert row is not None
        return RuleSystemDefinition.from_row(row), row.id

    # d20 with a real modifier resolved from actual entity/actor state.
    d20_system, d20_id = await _rule_system("generic_d20")
    d20_record = await resolve(
        tenant_id=tenant_a,
        session_id=sess.id,
        event_seq=0,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20+3",
        check_type="strength_check",
        actor_fields={"strength": 16},
        target=15,
        rule_system=d20_system,
        rule_system_id=d20_id,
        legal_check_types=None,
    )
    assert d20_record.outcome in ("success", "failure")
    assert d20_record.modifiers["total"] == 3  # (16 - 10) / 2

    # A three-band result. Defined here rather than shipped: the claim is that a banded
    # system resolves through unchanged core, and that is a property of the *shape*, not of
    # any publisher's game -- the pack itself names no third-party system any more.
    from core.resolution.rule_system import RuleSystemDefinitionSchema, create_rule_system

    banded_key = f"banded{uuid.uuid4().hex[:8]}"
    await create_rule_system(
        tenant_a,
        RuleSystemDefinitionSchema(
            key=banded_key,
            name="Banded 2d6",
            expression_grammar={
                "allowed_sides": [6],
                "max_term_count": 2,
                "allow_keep_drop": False,
            },
            check_types=["move"],
            outcome_bands=[
                {"min": 2, "max": 6, "outcome": "miss"},
                {"min": 7, "max": 9, "outcome": "partial"},
                {"min": 10, "max": 13, "outcome": "hit"},
            ],
            modifier_resolver={"move": "has(fields.stat) ? fields.stat : 0"},
            validators=[],
        ),
    )
    banded_system, banded_id = await _rule_system(banded_key)
    banded_record = await resolve(
        tenant_id=tenant_a,
        session_id=sess.id,
        event_seq=1,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="2d6+1",
        check_type="move",
        actor_fields={"stat": 1},
        target=None,
        rule_system=banded_system,
        rule_system_id=banded_id,
        legal_check_types=None,
    )
    assert banded_record.outcome in ("miss", "partial", "hit")

    # Coin flip.
    coin_system, coin_id = await _rule_system("coin_flip")
    coin_record = await resolve(
        tenant_id=tenant_a,
        session_id=sess.id,
        event_seq=2,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d2+0",
        check_type="call",
        actor_fields={},
        target=None,
        rule_system=coin_system,
        rule_system_id=coin_id,
        legal_check_types=None,
    )
    assert coin_record.outcome in ("heads", "tails")


async def test_rpg_pack_has_zero_core_imports() -> None:
    """Mirrors ``tests/architecture/test_packs_independence.py``'s generic lint,
    scoped to this pack: trivially true (the pack is pure JSON), asserted directly so
    this acceptance criterion has its own named test rather than relying solely on the
    repo-wide lint."""
    py_files = list(_PACK_DIR.rglob("*.py"))
    assert py_files == []


async def test_pack_seed_is_library_read_only_with_fork_on_edit(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    # the library tenant, exercised against a pack seed that actually ships. It used to
    # be the rpg pack's, which was a placeholder for licensed reference text whose own note
    # said it had never been legally reviewed; that seed is gone and the mechanism is not,
    # so the test moved to the one shipped seed left rather than disappearing with it.
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    loaded = await load_pack(_SEEDED_PACK_DIR, tenant_a, workspace_id)
    assert "swdev-engineering-handbook" in loaded.seed_source_keys

    async with tenant_scope(LIBRARY_TENANT_ID) as session:
        from sqlalchemy import text as sa_text

        source_id = (
            await session.execute(
                sa_text("SELECT id FROM knowledge_source WHERE key = :key"),
                {"key": "swdev-engineering-handbook"},
            )
        ).scalar_one()

    # Tenant A (via load_pack above) can read the library source; a second,
    # independent tenant that never touched it can too -- the read side of the
    # disjunct, exercised against the pack's own real shipped content.
    tenant_a_read = await get_source(tenant_a, source_id)
    assert tenant_a_read is not None
    assert tenant_a_read.tenant_id == LIBRARY_TENANT_ID

    # Editing forks: a fresh, uniquely-keyed library source (not the shared pack seed
    # content, which accumulates forks across every prior run of this suite against
    # this repo's persistent dev database -- see core.knowledge.library.seed_library_
    # source's own docstring on why this content is shipped once, not per-test) proves
    # the same fork_if_library/fork_source mechanism with real content, independent of
    # that accumulation.
    fork_probe = await seed_library_source(
        f"rpg-fork-probe-{uuid.uuid4().hex[:8]}",
        "Fork Probe",
        "rules",
        [
            (
                "probe-entry",
                EntryFields(
                    title="Probe",
                    body_md="probe body",
                    class_="rules",
                    scope_key="workspace_public",
                ),
            )
        ],
    )
    forked_id = await fork_if_library(tenant_a, fork_probe.id)
    assert forked_id != fork_probe.id
    forked = await get_source(tenant_a, forked_id)
    assert forked is not None
    assert forked.tenant_id == tenant_a
