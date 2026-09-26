"""Deterministic entity injection: the real
``EntityStateRenderer`` for ``core.assembler.context_assembler.assemble()``, replacing
the Phase-1 no-op default. Entity state is injected because it is **current**, never
retrieved because it scored well against a query ('s lesson: state is not
knowledge) -- this module never touches ``core.knowledge.retrieval``/``search_and_
budget``, has no query text or embedding parameter, and runs as its own assembler step
entirely separate from the knowledge retrieval pipeline.

**Visibility.** Reuses ``core.assembler.visibility.scopes_for`` (the one INV-4
resolver every retrieval call already goes through) to decide which entities are
"relevant to the phase" -- every entity in the workspace whose ``scope_key`` is in the
viewer's resolved scope set. A field tagged ``private`` additionally requires
*its own* declared ``scope_key`` to be in that same set -- absent from the rendered
block entirely when it isn't, never blanked, so a viewer's client can't distinguish
"hidden" from "field doesn't exist" (the same contract the sheet renderer needs on
the wire).

**Known limitation.** Derived fields (``DerivedDef``) carry no tags/scope_key in the
model, so they're always rendered (computed from the entity's full, unfiltered data --
otherwise a derived expression referencing a field the viewer can't see would raise,
since ``compute_derived`` evaluates for real, not against dummy values). A derived value
that happens to reveal something about a private input is a gap this task doesn't close;
tightening it (tagging derived fields too) is additive, not a redesign, if a real pack
needs it.

**Stable rendering.** Entities and fields are rendered in sorted-key order -- the same
data always produces the same string, so a turn where entity state didn't change
doesn't invalidate the prompt-cache prefix.
"""

from __future__ import annotations

import uuid

from core.assembler.context_assembler import EntityStateBlock
from core.assembler.visibility import scopes_for
from core.entities.repo import get_schema
from core.entities.schema import EntitySchemaDefinition
from core.entities.storage import EntityRow, list_entities_for_scope
from core.entities.validation import compute_derived
from core.process.dsl.schema import PhaseSpec
from core.tenancy.models import Principal


def visible_fields(
    entity: EntityRow, definition: EntitySchemaDefinition, viewer_scopes: frozenset[str]
) -> dict[str, object]:
    visible: dict[str, object] = {}
    for field in definition.fields:
        if field.key not in entity.data:
            continue
        if "private" in field.tags and (
            field.scope_key is None or field.scope_key not in viewer_scopes
        ):
            continue
        visible[field.key] = entity.data[field.key]
    return visible


def _render_entity(
    entity: EntityRow, definition: EntitySchemaDefinition, viewer_scopes: frozenset[str]
) -> str:
    visible = visible_fields(entity, definition, viewer_scopes)
    derived = compute_derived(definition, entity.data)  # full data -- see module docstring

    field_parts = [f"{key}={visible[key]!r}" for key in sorted(visible)]
    derived_parts = [f"{key}={derived[key]!r}" for key in sorted(derived)]
    fsm_parts = [f"{key}:{entity.fsm_states[key]}" for key in sorted(entity.fsm_states)]

    body = ", ".join(field_parts + derived_parts)
    fsm_body = "; fsm[" + ", ".join(fsm_parts) + "]" if fsm_parts else ""
    header = f'<entity id="{entity.id}" key="{entity.key}" name="{entity.name}">'
    return f"{header}\n{body}{fsm_body}\n</entity>"


async def render_entity_state(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer: Principal,
    phase: PhaseSpec,
) -> EntityStateBlock:
    """Matches ``core.assembler.context_assembler.EntityStateRenderer``'s exact
    signature -- the real implementation of that seam."""
    scope_set = await scopes_for(tenant_id, viewer.id, workspace_id, phase.visibility, session_id)
    if not scope_set:
        return EntityStateBlock(rendered_text="", token_count=0)

    entities = await list_entities_for_scope(tenant_id, workspace_id, frozenset(scope_set))
    if not entities:
        return EntityStateBlock(rendered_text="", token_count=0)

    blocks: list[str] = []
    entity_versions: dict[str, int] = {}
    for entity in sorted(entities, key=lambda e: e.key):
        schema_row = await get_schema(tenant_id, entity.schema_id)
        assert schema_row is not None
        definition = schema_row.to_definition()
        blocks.append(_render_entity(entity, definition, frozenset(scope_set)))
        entity_versions[str(entity.id)] = entity.version

    rendered_text = "\n".join(blocks)
    return EntityStateBlock(
        rendered_text=rendered_text,
        token_count=len(rendered_text.split()),
        entity_versions=entity_versions,
    )
