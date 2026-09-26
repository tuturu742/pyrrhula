"""ContextManifest persistence + read access control (INV-10). Replay verification itself
(re-running ``assemble()`` from a stored manifest's
own recorded inputs and checking ``sha256(rendered) == rendered_hash``) is CI-blocking and
lives in ``tests/replay/``, not here -- this module owns writing a manifest and reading
one back, which the replay test (and the inspector) both build on.

**Not auto-wired into ``core.agents.runtime``.** Nothing in Phase 1 yet calls
``assemble()`` as part of an actual agent turn -- that integration needs C1.5/C1.6 (a real
rule system and resolution service) to exist first for a real phase to make sense, and no
task in this phase's list names that wiring as its own job. ``write_context_manifest`` is
a standalone, directly-callable function today, matching this project's established
injection-seam discipline: build the complete, tested piece now, wire it into the runtime
once the pieces it would coordinate with actually exist.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.assembler.context_assembler import ContextManifest, ManifestEntry, Redaction
from core.assembler.models import ContextManifestRow
from core.ports.permission import PermissionService, UnknownActionError
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope


class ManifestAccessDeniedError(Exception):
    """A principal tried to read a manifest they weren't the viewer of, and holds no
    workspace role granted ``read_any_manifest`` either."""


class ManifestNotFoundError(Exception):
    pass


def _entry_to_json(entry: ManifestEntry) -> dict[str, object]:
    return {
        "citation_id": entry.citation_id,
        "chunk_id": str(entry.chunk_id),
        "entry_id": str(entry.entry_id),
        "entry_key": entry.entry_key,
        "source_id": str(entry.source_id),
        "version_id": str(entry.version_id) if entry.version_id is not None else None,
        "class": entry.class_,
        "bucket": entry.bucket,
        "rank": entry.rank,
        "score": entry.score,
        "why": entry.why,
        "token_count": entry.token_count,
    }


def _redaction_to_json(redaction: Redaction) -> dict[str, object]:
    return {"type": redaction.type, "id": redaction.id, "reason": redaction.reason}


async def write_context_manifest(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    viewer_principal_id: uuid.UUID,
    phase_label: str,
    manifest: ContextManifest,
    *,
    behavior_profile_version: int | None = None,
) -> ContextManifestRow:
    """Append-only write (‡): at most one manifest per ``(session_id, event_seq)`` --
    enforced by the migration's ``UNIQUE`` constraint, matching the same ``event_seq`` the
    message it renders context for is (or will be) stamped with. Deliberately its own
    transaction rather than assuming co-transaction with a message write -- the caller
    decides how tightly to couple the two; see module docstring.

    ``behavior_profile_version`` : the version in effect for the acting
    agent at generation time, resolved by the caller (``core.behavior.repo
    .get_current_behavior_profile``) rather than looked up here -- this module has no
    opinion about which agent a manifest belongs to beyond what the caller already
    resolved for ``assemble()`` itself. ``None`` is legitimate: not every agent has a
    behavior profile (most don't, pre-E2.3, and low-stakes-only agents may never need
    one).

    Retry-safe: a resumed turn re-executes at the same ``event_seq`` (CLAUDE.md rule 8)
    after a failed first attempt that may already have appended this manifest -- the
    append-only table permits no UPDATE/DELETE, so the retry *reuses* the existing row
    rather than violating the unique constraint."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(ContextManifestRow).where(
                ContextManifestRow.session_id == session_id,
                ContextManifestRow.event_seq == event_seq,
            )
        )
        if existing is not None:
            return existing
        row = ContextManifestRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            viewer_principal_id=viewer_principal_id,
            phase=phase_label,
            entries=[_entry_to_json(e) for e in manifest.entries],
            redactions=[_redaction_to_json(r) for r in manifest.redactions],
            resolution_ids=list(manifest.resolution_ids),
            entity_versions=dict(manifest.entity_versions),
            behavior_profile_version=behavior_profile_version,
            token_counts=manifest.token_counts,
            rendered_hash=manifest.content_hash,
            history_summary_from_seq=manifest.history_summary_from_seq,
            history_summary_to_seq=manifest.history_summary_to_seq,
            history_summary_hash=manifest.history_summary_hash,
        )
        session.add(row)
        await session.flush()
        return row


async def get_manifest_for_message(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    message_id: uuid.UUID,
    requesting_principal_id: uuid.UUID,
    *,
    permission_service: PermissionService,
) -> ContextManifestRow:
    """the read path (-style access control, applied to manifests rather than
    secrets): the exact viewer of a manifest may always read it back; anyone else needs a
    workspace role granted the ``read_any_manifest`` action (facilitator/overseer by
    default -- see the C1.3 migration's ``role_permission`` seed), checked through the
    injected ``PermissionService`` port (CLAUDE.md rule 12: call sites depend on the port
    and never construct a specific adapter or inline role logic themselves)."""
    async with tenant_scope(tenant_id) as session:
        message = await session.get(MessageRow, message_id)
        if message is None or message.context_manifest_id is None:
            raise ManifestNotFoundError(f"no manifest for message {message_id}")
        manifest_row = await session.get(ContextManifestRow, message.context_manifest_id)
        if manifest_row is None:
            raise ManifestNotFoundError(f"no manifest for message {message_id}")

    if manifest_row.viewer_principal_id == requesting_principal_id:
        return manifest_row

    try:
        granted = await permission_service.check(
            tenant_id, requesting_principal_id, "read_any_manifest", "workspace", workspace_id
        )
    except UnknownActionError:
        granted = False
    if not granted:
        raise ManifestAccessDeniedError(
            f"principal {requesting_principal_id} may not read this manifest"
        )
    return manifest_row
