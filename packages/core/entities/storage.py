"""Entity storage (F3.3, plan §10.5, §12.5): one shared, JSONB-backed ``entity`` table
for every schema, with **generated columns** hoisted per-schema for fields marked
``indexed: true`` -- JSONB flexibility with real btree index performance on the two or
three fields a workspace actually queries (initiative, status, owner).

**Why one shared table, not one table per schema.** A schema is authored data (F3.1),
created and versioned at runtime by tenants and packs -- there is no fixed, compile-time
set of "entity types" to give their own tables. JSONB plus per-field generated columns is
how this project gets index performance without a runtime `CREATE TABLE`.

**Why generated columns are schema-conditional.** All entities share one table, so a
generated column for field key ``"status"`` under one schema must not also materialise
(and pollute the index of) a same-named-but-unrelated field under a different schema.
Each generated column's expression is `CASE WHEN schema_id = <this schema> THEN ... END`,
and its index is a **partial** index (`WHERE schema_id = <this schema>`) -- selective for
exactly the rows it's meant for, ignored (NULL, out of the index) for every other schema's
rows. The column name itself is further namespaced with a hash of the schema id
(``gc_<field>_<hash>``) so two different schemas can each index a field with the same key
without colliding on the generated column's own name.

**Why this needs ``core.tenancy.scope.admin_ddl_session``.** `ALTER TABLE`/`CREATE INDEX`
require privileges the RLS-restricted ``pyrrhula_app`` role does not have (and should
not be granted -- see that function's docstring). This is schema DDL, not a tenant data
read/write; RLS/tenant-scoping isn't even the right lens for it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.validation import validate_and_prepare_write
from core.tenancy.models import Base
from core.tenancy.scope import admin_ddl_session, tenant_scope

_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_PG_TYPE_BY_FIELD_TYPE: dict[str, str] = {
    "string": "text",
    "integer": "integer",
    "number": "double precision",
    "boolean": "boolean",
}


class EntityRow(Base):
    """One entity instance, any schema. ``fsm_states`` is the render-time projection
    F3.6 injects (``{"health": "bloodied", "quest": "active"}``); ``data`` holds raw
    field values only -- derived values are never stored (F3.1's design decision).
    Row-lock semantics for concurrent writes are F3.5's job, not this module's -- this
    task owns shape, not mutation policy."""

    __tablename__ = "entity"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    schema_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("entity_schema.id", ondelete="RESTRICT"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    data: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    fsm_states: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Which session filed this, when one did. Entities stay workspace-scoped -- a backlog
    # is shared and outlives any conversation -- but without recording where each came
    # from, every view of them is the same view, and a session panel showed a workspace's
    # whole accumulated history as if it were this session's work.
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "workspace_id", "key", name="uq_entity_tenant_ws_key"),
    )


def generated_column_name(schema_id: uuid.UUID, field_key: str) -> str:
    if not _FIELD_KEY_RE.match(field_key):
        raise ValueError(f"field key {field_key!r} is not a safe SQL identifier")
    return f"gc_{field_key}_{schema_id.hex[:8]}"


async def add_indexed_field_column(schema_id: uuid.UUID, field: FieldDef) -> None:
    """The generator's "upgrade": adds a schema-conditional generated column plus a
    partial btree index for one ``indexed: true`` field. Idempotent (``IF NOT EXISTS``)
    so re-running it for a field that's already indexed is a no-op, not an error."""
    if not field.indexed:
        return
    if field.type not in _PG_TYPE_BY_FIELD_TYPE:
        raise ValueError(
            f"field {field.key!r}: 'indexed' generated columns support "
            f"{sorted(_PG_TYPE_BY_FIELD_TYPE)}, not {field.type!r} "
            "(an array field needs a GIN/JSONB-path index, a different mechanism)"
        )
    pg_type = _PG_TYPE_BY_FIELD_TYPE[field.type]
    column = generated_column_name(schema_id, field.key)
    index = f"ix_entity_{column}"

    async with admin_ddl_session() as session:
        await session.execute(
            text(
                f'ALTER TABLE entity ADD COLUMN IF NOT EXISTS "{column}" {pg_type} '
                f"GENERATED ALWAYS AS ("
                f"CASE WHEN schema_id = '{schema_id}'::uuid "
                f"THEN (data->>'{field.key}')::{pg_type} ELSE NULL END"
                f") STORED"
            )
        )
        await session.execute(
            text(
                f'CREATE INDEX IF NOT EXISTS "{index}" ON entity ("{column}") '
                f"WHERE schema_id = '{schema_id}'::uuid"
            )
        )


async def drop_indexed_field_column(schema_id: uuid.UUID, field_key: str) -> None:
    """The generator's "downgrade": removing the ``indexed`` flag drops the generated
    column (Postgres cascades the drop to its own index automatically)."""
    column = generated_column_name(schema_id, field_key)
    async with admin_ddl_session() as session:
        await session.execute(text(f'ALTER TABLE entity DROP COLUMN IF EXISTS "{column}"'))


async def sync_indexed_columns(schema_id: uuid.UUID, definition: EntitySchemaDefinition) -> None:
    """Reconciles every field's ``indexed`` flag against the generated-column state.

    **Nothing calls this yet**, which means an ``indexed: true`` field on an entity schema
    is currently authored and then ignored -- no generated column, no index. Wiring it
    needs runtime DDL from the app role at schema-activation time, which is a decision
    rather than an oversight to quietly fix. Kept because the mechanism is right and the
    per-field helpers below are tested; the docstring used to claim it was "called
    whenever a schema version is activated", which was the misleading part.

    Not itself schema-version-aware
    beyond ``schema_id`` -- each version gets its own generated columns (its own
    ``schema_id``), so an older version's columns are simply left in place (harmless,
    inert once nothing references that version) rather than torn down."""
    for field in definition.fields:
        if field.indexed:
            await add_indexed_field_column(schema_id, field)
        else:
            await drop_indexed_field_column(schema_id, field.key)


async def create_entity(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    schema_id: uuid.UUID,
    schema_definition: EntitySchemaDefinition,
    key: str,
    name: str,
    scope_key: str,
    data: Mapping[str, object],
    origin_session_id: uuid.UUID | None = None,
) -> EntityRow:
    validated = validate_and_prepare_write(schema_definition, data)
    async with tenant_scope(tenant_id) as session:
        row = EntityRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            schema_id=schema_id,
            key=key,
            name=name,
            scope_key=scope_key,
            data=dict(validated),
            fsm_states={},
            version=1,
            origin_session_id=origin_session_id,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def get_entity(tenant_id: uuid.UUID, entity_id: uuid.UUID) -> EntityRow | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(EntityRow, entity_id)


async def list_entities_for_scope(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, scope_keys: frozenset[str]
) -> list[EntityRow]:
    """``scope_keys`` is required and defaultless (INV-4 discipline extended to
    entities): a caller cannot construct this call without deciding what's visible
    first -- there is no "give me everything" shorthand."""
    if not scope_keys:
        raise ValueError("scope_keys must be a non-empty set (INV-4)")
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            select(EntityRow).where(
                EntityRow.tenant_id == tenant_id,
                EntityRow.workspace_id == workspace_id,
                EntityRow.scope_key.in_(scope_keys),
            )
        )
        return list(result.scalars())


async def list_all_entities_for_workspace(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[EntityRow]:
    """Unfiltered by scope -- for provenance snapshots (F3.6's checkpoint entity-version
    pins), not for anything viewer-facing. Mirrors ``core.knowledge.authoring
    .list_workspace_attachments``'s identical "every attached source, not a viewer's
    visible subset" shape for ``knowledge_version_pins``: a checkpoint captures what
    exists for fork/resume correctness, independent of who's looking."""
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            select(EntityRow).where(
                EntityRow.tenant_id == tenant_id, EntityRow.workspace_id == workspace_id
            )
        )
        return list(result.scalars())
