"""Resolve a mechanical check and apply its consequence to an entity (P1).

One deterministic step that composes two existing, tested primitives without inventing new
storage: ``core.resolution.service.resolve`` (a check → an immutable, hash-chained
``ResolutionRecord``, INV-7) and ``core.entities.mutation`` (the single entity write path,
idempotent + append-only ``entity_state_change``). The record is the source of truth for the
outcome; the field change + FSM transition are the outcome's effect on the actor's state.

Domain-neutral: the caller supplies which machine/trigger to drive and which fields to set
(an RPG encounter passes ``machine_key="health"``, ``trigger="damage_taken"``,
``set_fields={"hit_points": ...}`` -- those RPG words live in pack data and the caller, never
here). Every write is attributed to the resolution record via ``cause_ref="resolution:<id>"``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from core.entities.mutation import mutate, transition
from core.ports.permission import PermissionService
from core.resolution.rule_system import RuleSystemDefinition
from core.resolution.service import resolve


async def resolve_and_apply(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    tool_key: str,
    actor_entity_id: uuid.UUID,
    expression: str,
    check_type: str,
    machine_key: str,
    trigger: str,
    set_fields: Mapping[str, object],
    rule_system: RuleSystemDefinition,
    rule_system_id: uuid.UUID,
    legal_check_types: frozenset[str] | None,
    permission_service: PermissionService,
    actor_fields: Mapping[str, object] | None = None,
    target: int | None = None,
) -> dict[str, Any]:
    record = await resolve(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=event_seq,
        tool_key=tool_key,
        actor_entity_id=actor_entity_id,
        expression=expression,
        check_type=check_type,
        actor_fields=dict(actor_fields or {}),
        target=target,
        rule_system=rule_system,
        rule_system_id=rule_system_id,
        legal_check_types=legal_check_types,
    )
    cause_ref = f"resolution:{record.id}"

    if set_fields:
        await mutate(
            principal_id,
            tenant_id,
            workspace_id,
            actor_entity_id,
            dict(set_fields),
            cause="tool",
            cause_ref=cause_ref,
            idempotency_key=f"apply-fields:{session_id}:{event_seq}",
            permission_service=permission_service,
            session_id=session_id,
            event_seq=event_seq,
        )

    transition_result = await transition(
        principal_id,
        tenant_id,
        workspace_id,
        actor_entity_id,
        machine_key,
        trigger,
        idempotency_key=f"apply-fsm:{session_id}:{event_seq}",
        permission_service=permission_service,
        cause="tool",
        cause_ref=cause_ref,
        session_id=session_id,
        event_seq=event_seq,
    )

    return {
        "resolution_record_id": str(record.id),
        "outcome": record.outcome,
        "transitioned": transition_result["transitioned"],
        "new_state": transition_result["new_state"],
    }
