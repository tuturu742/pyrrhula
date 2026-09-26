"""ProcessDefinition authoring: create (= publish) a new immutable version, list,
and fetch. A definition that fails validation is **never persisted** -- ``create_definition``
raises before touching the database, so every row that does exist in ``process_definition``
is one the interpreter can trust by construction; there is no draft/invalid state to
accidentally read. Dry-run validation (for an editor giving live feedback, D1.2) is
``validate_document``, which never persists anything at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from core.process.dsl.validator import ValidationIssue, validate_raw
from core.process.models import ProcessDefinitionRow
from core.tenancy.scope import tenant_scope


class DefinitionValidationError(Exception):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__(f"{len(issues)} validation issue(s): {issues}")


def validate_document(raw: dict[str, object]) -> list[ValidationIssue]:
    _dsl, issues = validate_raw(raw)
    return issues


async def _next_version(tenant_id: uuid.UUID, workspace_id: uuid.UUID | None, key: str) -> int:
    async with tenant_scope(tenant_id) as session:
        # SQLAlchemy compiles `Column == None` to `IS NULL`, so this is NULL-safe for
        # tenant-template (workspace_id IS NULL) rows without a manual IS NOT DISTINCT FROM.
        current_max = await session.scalar(
            select(func.max(ProcessDefinitionRow.version)).where(
                ProcessDefinitionRow.tenant_id == tenant_id,
                ProcessDefinitionRow.workspace_id == workspace_id,
                ProcessDefinitionRow.key == key,
            )
        )
    return (current_max or 0) + 1


async def create_definition(
    tenant_id: uuid.UUID,
    key: str,
    name: str,
    definition_raw: dict[str, object],
    *,
    workspace_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
) -> ProcessDefinitionRow:
    dsl, issues = validate_raw(definition_raw)
    if issues or dsl is None:
        raise DefinitionValidationError(issues)

    version = await _next_version(tenant_id, workspace_id, key)

    async with tenant_scope(tenant_id) as session:
        row = ProcessDefinitionRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            key=key,
            version=version,
            name=name,
            definition=dsl.model_dump(mode="json", by_alias=True, exclude_none=True),
            validated_at=datetime.now(UTC),
            validation_errors=[],
            created_by=created_by,
        )
        session.add(row)
        await session.flush()
        return row


async def get_definition(
    tenant_id: uuid.UUID, definition_id: uuid.UUID
) -> ProcessDefinitionRow | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(ProcessDefinitionRow, definition_id)


async def list_definitions(
    tenant_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> list[ProcessDefinitionRow]:
    """All versions, ordered by key then version -- a caller wanting "the latest" for a
    given key picks the highest-version row for it; the interpreter always resolves a
    specific pinned (id, version), never "latest", so no such helper is needed here."""
    async with tenant_scope(tenant_id) as session:
        stmt = select(ProcessDefinitionRow).where(ProcessDefinitionRow.tenant_id == tenant_id)
        if workspace_id is not None:
            stmt = stmt.where(ProcessDefinitionRow.workspace_id == workspace_id)
        if not include_archived:
            stmt = stmt.where(ProcessDefinitionRow.archived_at.is_(None))
        stmt = stmt.order_by(ProcessDefinitionRow.key, ProcessDefinitionRow.version)
        rows = (await session.execute(stmt)).scalars()
        return list(rows)


async def archive_definition(tenant_id: uuid.UUID, definition_id: uuid.UUID) -> None:
    """Soft-delete one process-definition version row: stamp ``archived_at`` so it drops out
    of ``list_definitions`` and the launch picker. Sessions already pinned to it keep running
    (they resolve the row by id directly); this only hides it from new launches."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ProcessDefinitionRow, definition_id)
        if row is None:
            raise ValueError(f"no process definition {definition_id} in this tenant")
        if row.archived_at is None:
            row.archived_at = datetime.now(UTC)
        await session.flush()
