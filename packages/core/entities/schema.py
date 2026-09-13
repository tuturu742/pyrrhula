"""EntitySchema (F3.1, plan §10.1/§10.2 (D7), §12.5): the generic Entity Schema shared by
every domain -- a D&D character, a support ticket, and a software work item are the same
object; nothing in core knows what HP is. Typed fields as a JSON Schema 2020-12 subset,
derived fields and cross-field constraints as CEL (bounded, no I/O, safe for untrusted
expressions in a shared process), versioned per workspace or pack-provided.

**Explicitly rejected alternatives (§10.2):**
- **RestrictedPython** -- sandbox escapes are a well-known, recurring class of bug; "the
  sandbox is unbreakable" is not a boundary this project is willing to bet INV-8/tenancy
  on, and CLAUDE.md rule 10 forbids user-authored code categorically, not just "risky"
  code.
- **JsonLogic** -- expressive enough for simple predicates but too weak for real derived
  fields (`ceil(sqrt(fields.xp / 100))`) without inventing an ad hoc function-call
  extension that would just become a second, worse expression language.
- **Starlark** -- Q8's fallback if CEL doesn't survive contact with real pack authoring;
  deliberately *not* adopted pre-emptively. The phase-3 exit review (Q8, revisited once
  three packs' worth of guards/deriveds/constraints/merge-gates/checklists exist) is where
  this gets revisited with evidence, not before.

``state_machines``/``views`` were both amended forward in place -- F3.2
(``core.entities.fsm.StateMachineDef``) and F3.4 (``core.entities.views.ViewDef``) --
the "amend forward" pattern ``docs/phase-workflow.md`` names for a later task revising
an earlier one's already-shipped shape, rather than F3.1 guessing their content early.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.fsm import StateMachineDef
from core.entities.views import ViewDef
from core.tenancy.models import Base

# The JSON Schema 2020-12 subset this product accepts for a field's own type -- no
# `$ref` recursion, no remote refs (§10.1): a closed, boring set of primitive shapes.
FieldType = Literal["string", "integer", "number", "boolean", "array"]

_DUMMY_TYPE_FOR_CEL: dict[FieldType, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
}


class FieldDef(BaseModel):
    """One typed field. ``tags``/``tag_metadata`` are validated against F3.4's fixed
    vocabulary in that task's hookup (this model accepts any string tag structurally;
    F3.1 alone doesn't know the tag vocabulary yet). ``scope_key`` marks a field
    ``private`` at the per-field grain (F3.4's ``private`` tag contract) -- ``None`` means
    the field participates in the entity's own top-level visibility only."""

    model_config = ConfigDict(extra="forbid")

    key: str
    type: FieldType
    items: Literal["string", "integer", "number"] | None = None
    enum: list[str] | list[int] | None = None
    minimum: float | None = None
    maximum: float | None = None
    indexed: bool = False
    tags: list[str] = []
    tag_metadata: dict[str, object] = {}
    scope_key: str | None = None

    @model_validator(mode="after")
    def _items_only_for_array(self) -> FieldDef:
        if self.type == "array" and self.items is None:
            raise ValueError(f"field {self.key!r}: type 'array' requires 'items'")
        if self.type != "array" and self.items is not None:
            raise ValueError(f"field {self.key!r}: 'items' is only valid for type 'array'")
        return self


class DerivedDef(BaseModel):
    """A field computed from other fields, never stored as an authored value (design
    decision: storing a derived value invites drift; recomputation is trivial at entity
    scale). ``type`` is declared so both the CEL compile-check (dummy activation) and the
    real write path (result coercion, mirroring
    ``core.process.interpreter``'s ``_COERCE_BY_TYPE``) know how to type the result."""

    model_config = ConfigDict(extra="forbid")

    key: str
    type: Literal["string", "integer", "number", "boolean"]
    expression: str


class ConstraintDef(BaseModel):
    """A CEL predicate over ``fields.*`` (raw and derived) that must hold for a write to
    succeed. ``message`` is optional human-facing context; the expression itself is
    always named in a violation, so a constraint need not restate itself to be
    diagnosable."""

    model_config = ConfigDict(extra="forbid")

    expression: str
    message: str | None = None


class EntitySchemaDefinition(BaseModel):
    """The authored document -- the composition object §10.1 describes. ``key``/
    ``version`` live on the ``entity_schema`` row, not here, mirroring
    ``ProcessDefinitionDSL``'s identical split between authored content and
    row/versioning metadata."""

    model_config = ConfigDict(extra="forbid")

    fields: list[FieldDef]
    derived: list[DerivedDef] = []
    constraints: list[ConstraintDef] = []
    state_machines: list[StateMachineDef] = []
    views: list[ViewDef] = []

    @model_validator(mode="after")
    def _unique_keys(self) -> EntitySchemaDefinition:
        field_keys = [f.key for f in self.fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError(f"duplicate field keys: {field_keys}")
        derived_keys = [d.key for d in self.derived]
        if len(derived_keys) != len(set(derived_keys)):
            raise ValueError(f"duplicate derived keys: {derived_keys}")
        overlap = set(field_keys) & set(derived_keys)
        if overlap:
            raise ValueError(f"derived field key(s) collide with raw field keys: {sorted(overlap)}")
        return self

    def field_types(self) -> dict[str, str]:
        """``fields.*`` dummy-activation types for CEL compile-checking: raw fields plus
        derived fields, since a constraint may legally reference a derived value (§10.1's
        own example: ``fields.dexterity >= 13 && fields.level >= 3``)."""
        types = {f.key: _DUMMY_TYPE_FOR_CEL[f.type] for f in self.fields}
        types.update({d.key: d.type for d in self.derived})
        return types


class EntitySchemaRow(Base):
    """One immutable version of one schema, tenant-scoped. ``workspace_id`` is nullable
    for pack-provided schemas that aren't tied to a single workspace (mirroring
    ``rule_system``'s tenant-wide, not-workspace-scoped shape) -- a pack schema still
    lives under a concrete tenant (the library tenant for shipped-pack seed content, or a
    consuming tenant that customised a copy), just not pinned to one workspace within it.
    ``entity.schema_id`` (F3.3) FKs directly to a specific row here -- there is no separate
    "current version" catalog table; each save is a new row, and a caller pins whichever
    row id it means."""

    __tablename__ = "entity_schema"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    derived: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    state_machines: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    views: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    constraints: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # F3.12: who saved this version (a human's own edit, or the human who approved an
    # AI-drafted proposal -- `ai_assisted` distinguishes which). Both nullable: F3.1's
    # own pack-loading path (`core.packs.loader`) saves schemas with neither, the same
    # "no human in the loop yet" shape `entity_state_change.session_id` already accepts.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    ai_assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "workspace_id", "key", "version", name="uq_entity_schema_tenant_ws_key_ver"
        ),
    )

    def to_definition(self) -> EntitySchemaDefinition:
        return EntitySchemaDefinition.model_validate(
            {
                "fields": self.fields,
                "derived": self.derived,
                "constraints": self.constraints,
                "state_machines": self.state_machines,
                "views": self.views,
            }
        )
