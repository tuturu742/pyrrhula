"""Export job handler (G4.5) -- the worker-side wiring ``core.portability.export`` needs
but can't import itself (composition root: which ``BlobStore``, ``Encryptor``, and
``PermissionService`` adapter). Registered in ``worker.main``'s dispatch table under
``"export_workspace"``.

Export is a job rather than a synchronous download for the reason §11.2 implies and a
year-old workspace makes obvious: a bundle spans every session, every knowledge version,
and every chunk body in the workspace, and none of that belongs inside an HTTP request.
The handler returns a blob key; the API hands it back for the caller to poll and fetch.

Idempotent on ``(workspace, viewer, mode, requested_at)``: re-running a delivered job
returns the same blob key rather than paying for the whole walk twice. ``requested_at`` is
in the key deliberately -- two deliberate exports of the same workspace *should* produce
two bundles, because the workspace changed in between, and an idempotency key that
collapsed them would be wrong rather than merely thrifty.
"""

from __future__ import annotations

import uuid
from typing import Any

from adapters.permission.role_permission import RolePermissionService
from core.actions.idempotency import idempotent
from core.portability.export import DEFAULT_SECTIONS, ExportOptions, export_workspace
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from worker.blob_store_factory import get_blob_store
from worker.encryptor_factory import get_encryptor


def _export_job_key(**kwargs: Any) -> str:
    return (
        f"export_workspace:{kwargs['workspace_id']}:{kwargs['viewer_principal_id']}:"
        f"{kwargs['mode']}:{kwargs['requested_at']}"
    )


@idempotent(key_fn=_export_job_key)
async def run_export_job(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer_principal_id: uuid.UUID,
    mode: str,
    sections: list[str] | None = None,
    password: str | None = None,
    requested_at: str,
) -> dict[str, Any]:
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, viewer_principal_id)
        if viewer is None:
            raise ValueError(f"no principal {viewer_principal_id} in this tenant")
        session.expunge(viewer)

    result = await export_workspace(
        viewer,
        tenant_id,
        workspace_id,
        options=ExportOptions(
            mode=mode,  # type: ignore[arg-type]
            sections=frozenset(sections) if sections else DEFAULT_SECTIONS,
            password=password or None,
        ),
        encryptor=get_encryptor(),
        permission_service=RolePermissionService(),
    )

    blob_key = f"exports/{tenant_id}/{workspace_id}/{requested_at}-{viewer_principal_id}.pyr"
    await get_blob_store().put(blob_key, result.data, content_type="application/zip")
    return {
        "blob_key": blob_key,
        "bytes": len(result.data),
        "file_count": len(result.manifest["contents"]),
        "redaction_count": len(result.manifest["redactions"]),
    }


async def handle_export_workspace(payload: dict[str, Any]) -> dict[str, Any]:
    return await run_export_job(
        tenant_id=uuid.UUID(payload["tenant_id"]),
        workspace_id=uuid.UUID(payload["workspace_id"]),
        viewer_principal_id=uuid.UUID(payload["viewer_principal_id"]),
        mode=str(payload["mode"]),
        sections=list(payload.get("sections") or []) or None,
        password=(
            get_encryptor().decrypt(str(payload["password_sealed"]))
            if payload.get("password_sealed")
            else None
        ),
        requested_at=str(payload["requested_at"]),
    )
