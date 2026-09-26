"""EntityMutationService: the single write path for entity
data and FSM transitions. Every caller -- tools, process effects, human edits, imports,
and later the delegation outcomes -- goes through ``mutate()``/``transition()``, never
touches ``core.entities.storage``/``core.entities.fsm`` directly for a write.

**Concurrency.** ``SELECT ... FOR UPDATE`` on the entity row inside one transaction:
a second concurrent caller for the same entity blocks until the first commits, so
"exactly one state-change row per applied transition, versions strictly monotonic" is a
property of holding the lock for the whole guard-evaluate-effects-append sequence, not
something bolted on after. ``expected_version`` is a separate, optional check for a
caller that read the version across two round trips (e.g. a UI) and wants a stale write
rejected explicitly, rather than silently applied on top of a change it never saw.

**Idempotency (CLAUDE.md rule 8).** Wraps ``core.actions.idempotency.idempotent`` --
the same claim-before-execute primitive every other side-effecting operation in this
codebase uses (an atomic ``INSERT ... ON CONFLICT DO NOTHING`` before running the body,
so two concurrent retries can't both pass a check-then-run race). The caller supplies
the idempotency key directly (``(session_id, event_seq, attempt_target)``, per the task
description) rather than this module deriving one -- callers with no session
(the future out-of-session mutations) still need a stable key of their own choosing.

**Permission check precedes the lock.** ``PermissionService.check()`` runs before the
row is ever touched (never inline role logic, rule 12) -- a denied caller never blocks
a legitimate one waiting on the same row.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from core.actions.idempotency import idempotent
from core.entities.cel import evaluate
from core.entities.fsm import (
    ANY_STATE,
    EffectDef,
    EntityStateChangeRow,
    StateMachineDef,
    eligible_transitions,
    guard_passes,
)
from core.entities.repo import get_latest_schema_version, get_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import EntityRow, create_entity
from core.entities.validation import COERCE_BY_TYPE, compute_derived, validate_and_prepare_write
from core.ports.permission import PermissionService
from core.tenancy.scope import tenant_scope

_DEFAULT_INT = 10  # a playable middle value for an unbounded/only-floored integer field


class SchemaNotFoundError(Exception):
    pass


def _default_for_field(field: FieldDef) -> object:
    """A type-safe default for a field the caller left unset, derived from the schema alone
    (no per-domain field names here -- vocabulary rule 1). Integers land in-range: the
    midpoint when both bounds are given (so a 1..20 ability defaults to ~10), else a modest
    floored value; arrays empty; strings/text empty; booleans false; numbers 0."""
    if field.type == "integer" or field.type == "number":
        lo = field.minimum
        hi = field.maximum
        if lo is not None and hi is not None:
            value: float = (lo + hi) / 2
        elif hi is not None:
            value = hi
        else:
            value = max(lo or 0, _DEFAULT_INT)
        return int(value) if field.type == "integer" else value
    if field.type == "array":
        return []
    if field.type == "boolean":
        return False
    return ""  # string / text


class PermissionDeniedError(Exception):
    pass


class StaleVersionError(Exception):
    def __init__(self, entity_id: uuid.UUID, expected: int, actual: int) -> None:
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"entity {entity_id}: expected version {expected}, found {actual}")


class EntityNotFoundError(Exception):
    pass


class MachineNotFoundError(Exception):
    pass


def _field_types(definition: EntitySchemaDefinition) -> dict[str, str]:
    return {f.key: f.type for f in definition.fields}


def _apply_effects(
    data: dict[str, object],
    effects: list[EffectDef],
    combined_fields: Mapping[str, object],
    field_types: Mapping[str, str],
    side_effects: list[dict[str, object]],
) -> None:
    """Runs one state/transition's declared effects in order, mutating ``data`` in
    place. ``emit_event``/``invoke_tool`` have no wired consumer yet in this phase (no
    event bus, no tool registry reachable from here without inventing one) -- recorded
    into ``side_effects`` (visible in the mutation's own result) rather than silently
    dropped or fabricating infrastructure this module doesn't own."""
    for effect in effects:
        if effect.kind == "set_field":
            assert effect.field is not None and effect.value is not None
            raw = evaluate(effect.value, {**combined_fields, **data})
            field_type = field_types.get(effect.field)
            data[effect.field] = COERCE_BY_TYPE[field_type](raw) if field_type else raw
        elif effect.kind == "apply_modifier":
            assert effect.field is not None and effect.value is not None
            delta = evaluate(effect.value, {**combined_fields, **data})
            field_type = field_types.get(effect.field)
            current = data.get(effect.field, 0)
            new_value = current + delta  # type: ignore[operator]
            data[effect.field] = COERCE_BY_TYPE[field_type](new_value) if field_type else new_value
        elif effect.kind == "emit_event":
            assert effect.event is not None
            side_effects.append({"kind": "emit_event", "event": effect.event})
        elif effect.kind == "invoke_tool":
            assert effect.tool_key is not None
            side_effects.append(
                {"kind": "invoke_tool", "tool_key": effect.tool_key, "args": effect.tool_args}
            )
        elif effect.kind == "transition_other":
            assert effect.machine is not None and effect.trigger is not None
            side_effects.append(
                {"kind": "transition_other", "machine": effect.machine, "trigger": effect.trigger}
            )


async def _mutate_inner(
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    changes: Mapping[str, object],
    cause: str,
    cause_ref: str | None,
    session_id: uuid.UUID | None,
    event_seq: int | None,
    idempotency_key: str,
    expected_version: int | None,
) -> dict[str, Any]:
    del idempotency_key  # consumed by the @idempotent wrapper's key_fn, not the body

    async with tenant_scope(tenant_id) as session:
        entity_row = await session.scalar(
            select(EntityRow).where(EntityRow.id == entity_id).with_for_update()
        )
        if entity_row is None:
            raise EntityNotFoundError(f"no entity {entity_id}")
        if expected_version is not None and entity_row.version != expected_version:
            raise StaleVersionError(entity_id, expected_version, entity_row.version)

        schema_row = await get_schema(tenant_id, entity_row.schema_id)
        assert schema_row is not None
        definition = schema_row.to_definition()

        new_data = {**entity_row.data, **changes}
        validated = validate_and_prepare_write(definition, new_data)

        old_values = {key: entity_row.data.get(key) for key in changes}
        entity_row.data = dict(validated)
        entity_row.version += 1

        for key, new_value in changes.items():
            session.add(
                EntityStateChangeRow(
                    tenant_id=tenant_id,
                    entity_id=entity_id,
                    session_id=session_id,
                    event_seq=event_seq,
                    field_path=key,
                    old_value=old_values[key],
                    new_value=new_value,
                    cause=cause,
                    cause_ref=cause_ref,
                )
            )
        await session.flush()
        return {"entity_id": str(entity_id), "version": entity_row.version, "data": entity_row.data}


_mutate_idempotent = idempotent(key_fn=lambda **kwargs: str(kwargs["idempotency_key"]))(
    _mutate_inner
)


async def mutate(
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    changes: Mapping[str, object],
    cause: str,
    cause_ref: str | None,
    idempotency_key: str,
    *,
    permission_service: PermissionService,
    session_id: uuid.UUID | None = None,
    event_seq: int | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    if not await permission_service.check(
        tenant_id, principal_id, "entity:mutate", "workspace", workspace_id
    ):
        raise PermissionDeniedError(
            f"principal {principal_id} may not mutate entities in workspace {workspace_id}"
        )
    return await _mutate_idempotent(
        tenant_id=tenant_id,
        entity_id=entity_id,
        changes=changes,
        cause=cause,
        cause_ref=cause_ref,
        session_id=session_id,
        event_seq=event_seq,
        idempotency_key=idempotency_key,
        expected_version=expected_version,
    )


async def _create_inner(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    schema_key: str,
    entity_key: str,
    name: str,
    fields: Mapping[str, object],
    scope_key: str,
    idempotency_key: str,
    origin_session_id: uuid.UUID | None,
) -> dict[str, Any]:
    del idempotency_key  # consumed by the @idempotent wrapper's key_fn

    schema_row = await get_latest_schema_version(tenant_id, workspace_id, schema_key)
    if schema_row is None:
        # Schemas may be pinned workspace-wide (workspace_id) or tenant-wide (None).
        schema_row = await get_latest_schema_version(tenant_id, None, schema_key)
    if schema_row is None:
        raise SchemaNotFoundError(f"no schema {schema_key!r} in this workspace/tenant")
    definition = schema_row.to_definition()

    # Fill every field the caller omitted with a schema-derived default, so a partial
    # create (a model that supplied only a name + a couple of stats) still validates.
    data: dict[str, object] = dict(fields)
    for field in definition.fields:
        if field.key not in data or data[field.key] is None:
            data[field.key] = _default_for_field(field)

    row = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        entity_key,
        name,
        scope_key,
        data,
        origin_session_id=origin_session_id,
    )
    return {
        "entity_id": str(row.id),
        "key": row.key,
        "name": row.name,
        "schema_key": schema_key,
        "version": row.version,
        "fsm_states": row.fsm_states,
    }


_create_idempotent = idempotent(key_fn=lambda **kwargs: str(kwargs["idempotency_key"]))(
    _create_inner
)


async def create(
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    schema_key: str,
    entity_key: str,
    name: str,
    fields: Mapping[str, object],
    scope_key: str,
    idempotency_key: str,
    *,
    permission_service: PermissionService,
    origin_session_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Create one entity instance of ``schema_key``. Distinct permission from ``mutate``
    (``entity:create``, rule 12). Idempotent on ``idempotency_key`` -- a retry returns the
    first result instead of raising on the ``(tenant, workspace, key)`` unique constraint.
    ``scope_key`` is caller-supplied here but MUST be engine-derived by the tool/route that
    calls this (never model-chosen -- INV-4 visibility), same discipline as every other
    scoped write."""
    if not await permission_service.check(
        tenant_id, principal_id, "entity:create", "workspace", workspace_id
    ):
        raise PermissionDeniedError(
            f"principal {principal_id} may not create entities in workspace {workspace_id}"
        )
    return await _create_idempotent(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        schema_key=schema_key,
        entity_key=entity_key,
        name=name,
        fields=fields,
        scope_key=scope_key,
        idempotency_key=idempotency_key,
        origin_session_id=origin_session_id,
    )


def _find_machine(definition: EntitySchemaDefinition, machine_key: str) -> StateMachineDef:
    for machine in definition.state_machines:
        if machine.key == machine_key:
            return machine
    raise MachineNotFoundError(f"no state machine {machine_key!r} on this entity's schema")


async def _transition_inner(
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    machine_key: str,
    trigger: str,
    cause: str,
    cause_ref: str | None,
    session_id: uuid.UUID | None,
    event_seq: int | None,
    idempotency_key: str,
    expected_version: int | None,
) -> dict[str, Any]:
    del idempotency_key

    async with tenant_scope(tenant_id) as session:
        entity_row = await session.scalar(
            select(EntityRow).where(EntityRow.id == entity_id).with_for_update()
        )
        if entity_row is None:
            raise EntityNotFoundError(f"no entity {entity_id}")
        if expected_version is not None and entity_row.version != expected_version:
            raise StaleVersionError(entity_id, expected_version, entity_row.version)

        schema_row = await get_schema(tenant_id, entity_row.schema_id)
        assert schema_row is not None
        definition = schema_row.to_definition()
        machine = _find_machine(definition, machine_key)

        current_state = entity_row.fsm_states.get(machine_key, machine.initial)
        field_types = _field_types(definition)

        # Guard/effects evaluate over raw + derived fields (the constraint-check
        # convention): a guard may legally reference a derived value.
        derived = compute_derived(definition, entity_row.data)
        combined_fields: dict[str, object] = {**entity_row.data, **derived}

        applied: object = None
        for candidate in eligible_transitions(machine, current_state, trigger):
            if guard_passes(candidate, combined_fields):
                applied = candidate
                break

        if applied is None:
            return {
                "entity_id": str(entity_id),
                "transitioned": False,
                "new_state": current_state,
                "version": entity_row.version,
            }
        assert applied is not None

        data = dict(entity_row.data)
        side_effects: list[dict[str, object]] = []

        exit_state = next((s for s in machine.states if s.key == current_state), None)
        if exit_state is not None:
            _apply_effects(data, exit_state.on_exit, combined_fields, field_types, side_effects)

        transition_to = applied.to  # type: ignore[attr-defined]
        transition_effects = applied.effects  # type: ignore[attr-defined]
        _apply_effects(data, transition_effects, combined_fields, field_types, side_effects)

        enter_state = next(s for s in machine.states if s.key == transition_to)
        _apply_effects(data, enter_state.on_enter, combined_fields, field_types, side_effects)

        # Effects may have touched raw fields -- re-validate before committing, same
        # discipline as a plain mutate().
        validated = validate_and_prepare_write(definition, data)
        entity_row.data = dict(validated)

        new_fsm_states = dict(entity_row.fsm_states)
        new_fsm_states[machine_key] = transition_to
        entity_row.fsm_states = new_fsm_states
        entity_row.version += 1

        session.add(
            EntityStateChangeRow(
                tenant_id=tenant_id,
                entity_id=entity_id,
                session_id=session_id,
                event_seq=event_seq,
                field_path=f"fsm_states.{machine_key}",
                old_value=current_state,
                new_value=transition_to,
                cause=cause,
                cause_ref=cause_ref,
            )
        )
        await session.flush()
        return {
            "entity_id": str(entity_id),
            "transitioned": True,
            "new_state": transition_to,
            "version": entity_row.version,
            "side_effects": side_effects,
        }


_transition_idempotent = idempotent(key_fn=lambda **kwargs: str(kwargs["idempotency_key"]))(
    _transition_inner
)


async def transition(
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    machine_key: str,
    trigger: str,
    idempotency_key: str,
    *,
    permission_service: PermissionService,
    cause: str = "fsm",
    cause_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    event_seq: int | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    if not await permission_service.check(
        tenant_id, principal_id, "entity:mutate", "workspace", workspace_id
    ):
        raise PermissionDeniedError(
            f"principal {principal_id} may not transition entities in workspace {workspace_id}"
        )
    return await _transition_idempotent(
        tenant_id=tenant_id,
        entity_id=entity_id,
        machine_key=machine_key,
        trigger=trigger,
        cause=cause,
        cause_ref=cause_ref,
        session_id=session_id,
        event_seq=event_seq,
        idempotency_key=idempotency_key,
        expected_version=expected_version,
    )


__all__ = [
    "ANY_STATE",
    "EntityNotFoundError",
    "MachineNotFoundError",
    "PermissionDeniedError",
    "SchemaNotFoundError",
    "StaleVersionError",
    "create",
    "mutate",
    "transition",
]
