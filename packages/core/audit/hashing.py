"""The hash-chain primitive shared by ``AuditService.append`` (writes) and
``verify_chain`` (reads) — one implementation, so the writer and the verifier can't
silently drift apart on what "the hash of a row" means.
"""

from __future__ import annotations

import hashlib
import uuid

from core.audit.canonical import canonical_json


def audit_payload(
    *,
    tenant_id: uuid.UUID,
    actor_principal_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    target_ids: list[uuid.UUID],
    query: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "tenant_id": str(tenant_id),
        "actor_principal_id": str(actor_principal_id),
        "action": action,
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id else None,
        "target_ids": sorted(str(t) for t in target_ids),
        "query": query,
    }


def compute_row_hash(prev_hash: str | None, payload: dict[str, object]) -> str:
    return hashlib.sha256(f"{prev_hash or ''}|{canonical_json(payload)}".encode()).hexdigest()
