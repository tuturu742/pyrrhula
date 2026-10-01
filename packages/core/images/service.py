"""An image's life: from a reference to a runtime a repo can pick.

One state machine for every image the platform promotes, wherever it came from:

    imported:  verifying -> smoke_testing -> ready
    built:     queued -> submitted -> building -> verifying -> smoke_testing -> ready
               (the building half arrives with external builders)
    any active state -> failed | cancelled

**Verifying** asks the registry itself for the digest, with the declared registry's read
credential when there is one; a builder's or a bundle's word for it is never enough.
**Smoke testing** runs ``core.images.smoke`` on the tenant's own execution engine, pulling
the digest-pinned reference exactly as a delegation would. **Ready** promotes the build:
the definition's name becomes a tenant runtime whose image is ``…@sha256:…``, so it can
never drift to whatever a tag points at later.

The verification itself runs in the worker (``verify_image_build`` jobs). A build waiting
in ``verifying`` with no job is picked up by ``sweep_image_builds``, so a bundle import --
which has no job queue to hand -- and a worker that died mid-check end up on the same path.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.images.models import (
    ACTIVE_BUILD_STATUSES,
    ImageBuildRow,
    ImageDefinitionRow,
)
from core.ports.encryptor import Encryptor
from core.ports.exec_env import ExecEnvProvider, ExecEnvUnavailableError
from core.ports.image_registry import RegistryAuth, RegistryClient, RegistryError
from core.ports.job_queue import JobQueue
from core.repos.image_ref import (
    ImageRefError,
    canonical,
    is_digest_pinned,
    normalise_image_ref,
    registry_host,
    serveraddress_for,
)
from core.tenancy.scope import tenant_scope, unscoped_session

log = structlog.get_logger()

VERIFY_JOB_KIND = "verify_image_build"

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_DOCKERFILE_BYTES = 32 * 1024
# A check that has said nothing for this long is not coming back: its worker died, or the
# engine hung. Long enough for a slow pull of a large image on a one-shot engine.
_STALE_AFTER = timedelta(hours=1)


class ImageError(ValueError):
    """A request about an image that cannot be honoured. The message is for the user."""


class ImageNotFoundError(LookupError):
    pass


# ── views ───────────────────────────────────────────────────────────────────────────


def build_view(row: ImageBuildRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "origin": row.origin,
        "status": row.status,
        "target_ref": row.target_ref,
        "digest": row.digest,
        "pinned_ref": row.pinned_ref,
        "registry_key": row.registry_key,
        "builder_key": row.builder_key,
        "external_url": row.external_url,
        "harness_claim": dict(row.harness_claim or {}),
        "baked_harness": dict(row.baked_harness or {}),
        "smoke": row.smoke,
        "log_tail": row.log_tail,
        "error": row.error,
        "cancel_requested": row.cancel_requested,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


def _definition_view(
    row: ImageDefinitionRow, current: ImageBuildRow | None, latest: ImageBuildRow | None
) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "origin": row.origin,
        "harness_key": row.harness_key,
        "dockerfile": row.dockerfile,
        "current": build_view(current) if current is not None else None,
        "latest": build_view(latest) if latest is not None else None,
        "updated_at": row.updated_at,
    }


# ── helpers ─────────────────────────────────────────────────────────────────────────


def pinned_ref_for(ref: str, digest: str) -> str:
    """``host/repo@digest`` for any spelling of ``ref``: the tag, if any, is dropped."""
    full = canonical(ref)
    if "@" in full:
        full = full.split("@", 1)[0]
    head, _, last = full.rpartition("/")
    if ":" in last:
        last = last.split(":", 1)[0]
    return f"{head}/{last}@{digest}"


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME.match(name):
        raise ImageError(
            "an image name is 1-63 characters of lowercase letters, digits and '-', "
            "starting with a letter or digit"
        )
    from core.repos.runtimes import BUILTIN_RUNTIMES, CUSTOM

    if name == CUSTOM or name in BUILTIN_RUNTIMES:
        raise ImageError(f"{name!r} is a built-in runtime name; give the image another name")
    return name


async def _registry_for(ref: str) -> Any | None:
    """The declared registry this reference lives in, or None for an undeclared one."""
    from core.images.namespace import canon_host
    from core.images.registries import list_registries

    host = canon_host(registry_host(ref))
    for registry in await list_registries():
        if registry.enabled and host in registry.namespace().hosts():
            return registry
    return None


async def _own_namespace_auth(
    tenant_id: uuid.UUID, ref: str, *, encryptor: Encryptor
) -> RegistryAuth | None:
    """The declared registry's read credential -- but only for a reference inside the
    caller's *own* namespace there.

    The credential is the operator's, and it can usually read everything in that
    registry. Lending it to any reference on the host would let one organization read,
    through the platform, whatever else the operator keeps there: a path outside every
    managed root passes the namespace check when no allowlist is set. Inside its own
    namespace an organization can only reach its own images.
    """
    from core.images.namespace import own_roots

    registry = await _registry_for(ref)
    if registry is None or not registry.has_credential:
        return None
    full = canonical(ref)
    if not any(full.startswith(root) for root in own_roots(registry.namespace(), tenant_id)):
        return None
    from core.images.registries import registry_read_credential

    cred = await registry_read_credential(registry.key, encryptor=encryptor)
    return RegistryAuth(cred["username"], cred["password"]) if cred else None


async def registry_pull_auth(tenant_id: uuid.UUID, ref: str, *, encryptor: Encryptor) -> str | None:
    """``X-Registry-Auth`` for an engine pulling one of this organization's own images
    from a declared registry, or None. Same rule as verification: own namespace only."""
    auth = await _own_namespace_auth(tenant_id, ref, encryptor=encryptor)
    return _x_registry_auth(auth, ref)


async def _audit(
    tenant_id: uuid.UUID,
    actor: uuid.UUID | None,
    action: str,
    resource_id: uuid.UUID,
    query: dict[str, object],
) -> None:
    if actor is None:
        return
    from core.audit.service import AuditService

    await AuditService().append(
        tenant_id=tenant_id,
        actor_principal_id=actor,
        action=action,
        resource_type="image_build",
        resource_id=resource_id,
        query=query,
    )


# ── reads ───────────────────────────────────────────────────────────────────────────


async def list_images(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    async with tenant_scope(tenant_id) as session:
        definitions = (
            await session.scalars(
                select(ImageDefinitionRow)
                .where(
                    ImageDefinitionRow.tenant_id == tenant_id,
                    ImageDefinitionRow.archived_at.is_(None),
                )
                .order_by(ImageDefinitionRow.name)
            )
        ).all()
        out = []
        for definition in definitions:
            latest = await session.scalar(
                select(ImageBuildRow)
                .where(ImageBuildRow.definition_id == definition.id)
                .order_by(ImageBuildRow.created_at.desc())
                .limit(1)
            )
            current = (
                await session.get(ImageBuildRow, definition.current_build_id)
                if definition.current_build_id
                else None
            )
            out.append(_definition_view(definition, current, latest))
    return out


async def list_builds(tenant_id: uuid.UUID, name: str) -> list[dict[str, Any]]:
    async with tenant_scope(tenant_id) as session:
        definition = await _definition(session, tenant_id, name)
        rows = (
            await session.scalars(
                select(ImageBuildRow)
                .where(ImageBuildRow.definition_id == definition.id)
                .order_by(ImageBuildRow.created_at.desc())
                .limit(50)
            )
        ).all()
        return [{**build_view(r), "current": r.id == definition.current_build_id} for r in rows]


async def _definition(session: AsyncSession, tenant_id: uuid.UUID, name: str) -> ImageDefinitionRow:
    row = await session.scalar(
        select(ImageDefinitionRow).where(
            ImageDefinitionRow.tenant_id == tenant_id,
            ImageDefinitionRow.name == name,
            ImageDefinitionRow.archived_at.is_(None),
        )
    )
    if row is None:
        raise ImageNotFoundError(f"this organization has no image {name!r}")
    return row


# ── import ──────────────────────────────────────────────────────────────────────────


async def import_image(
    tenant_id: uuid.UUID,
    *,
    name: str,
    image: str,
    dockerfile: str = "",
    harness_claim: dict[str, Any] | None = None,
    requested_by: uuid.UUID | None,
    queue: JobQueue | None = None,
) -> dict[str, Any]:
    """Start using an exact, published image: a ``verifying`` build of definition ``name``.

    Refused before anything is written when the reference is not digest-pinned (a tag
    would let the image change after it was checked), or when it breaks the namespace or
    the operator's allowlist. ``dockerfile`` is provenance only -- how it was made, so an
    operator who forbids its registry can rebuild it -- and is never run.
    """
    name = _check_name(name)
    try:
        ref = normalise_image_ref(image) or ""
    except ImageRefError as exc:
        raise ImageError(f"image: {exc}") from exc
    if not is_digest_pinned(ref):
        raise ImageError(
            "an imported image must be pinned by digest (…@sha256:…); a tag can be moved "
            "to different contents after the image was checked"
        )
    from core.images.namespace import check_image_ref_for_tenant

    try:
        await check_image_ref_for_tenant(tenant_id, ref)
    except ImageRefError as exc:
        raise ImageError(str(exc)) from exc
    if len(dockerfile.encode()) > MAX_DOCKERFILE_BYTES:
        raise ImageError(f"the Dockerfile is larger than {MAX_DOCKERFILE_BYTES // 1024} KiB")
    claim: dict[str, str] = {}
    if harness_claim and str(harness_claim.get("key") or "").strip():
        claim = {
            "key": str(harness_claim["key"]).strip()[:63],
            "version": str(harness_claim.get("version") or "").strip()[:64],
        }

    from core.repos.runtimes import is_built, resolved_runtimes

    existing_runtime = (await resolved_runtimes(tenant_id)).get(name)
    if existing_runtime is not None and not is_built(existing_runtime):
        raise ImageError(
            f"this organization already has a runtime called {name!r}; give the image another name"
        )

    registry = await _registry_for(ref)
    try:
        async with tenant_scope(tenant_id) as session:
            definition = await session.scalar(
                select(ImageDefinitionRow).where(
                    ImageDefinitionRow.tenant_id == tenant_id,
                    ImageDefinitionRow.name == name,
                )
            )
            if definition is None:
                definition = ImageDefinitionRow(
                    tenant_id=tenant_id, name=name, origin="imported", created_by=requested_by
                )
                session.add(definition)
            elif definition.origin == "built" and definition.archived_at is None:
                raise ImageError(
                    f"{name!r} is an image this organization builds; import under another name"
                )
            definition.origin = "imported"
            definition.archived_at = None
            definition.dockerfile = dockerfile
            definition.harness_key = claim.get("key", "")
            definition.updated_by = requested_by
            await session.flush()
            build = ImageBuildRow(
                tenant_id=tenant_id,
                definition_id=definition.id,
                origin="imported",
                status="verifying",
                registry_key=registry.key if registry is not None else None,
                dockerfile=dockerfile,
                target_ref=ref,
                harness_claim=claim,
                requested_by=requested_by,
            )
            session.add(build)
            await session.flush()
            build_id = build.id
    except IntegrityError as exc:
        raise ImageError(f"{name!r} is already being checked; wait for it to finish") from exc

    await _audit(
        tenant_id, requested_by, "image.build.requested", build_id, {"name": name, "ref": ref}
    )
    if queue is not None:
        await _enqueue(queue, tenant_id, build_id)
    return await get_build(tenant_id, build_id)


async def get_build(tenant_id: uuid.UUID, build_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ImageBuildRow, build_id)
        if row is None:
            raise ImageNotFoundError("no such build")
        return build_view(row)


async def _enqueue(queue: JobQueue, tenant_id: uuid.UUID, build_id: uuid.UUID) -> None:
    job_id = await queue.enqueue(
        tenant_id, VERIFY_JOB_KIND, {"tenant_id": str(tenant_id), "build_id": str(build_id)}
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            update(ImageBuildRow)
            .where(ImageBuildRow.id == build_id, ImageBuildRow.job_id.is_(None))
            .values(job_id=job_id)
        )


# ── verification (worker) ───────────────────────────────────────────────────────────


async def _set(tenant_id: uuid.UUID, build_id: uuid.UUID, **values: Any) -> None:
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            update(ImageBuildRow).where(ImageBuildRow.id == build_id).values(**values)
        )


async def _fail(tenant_id: uuid.UUID, build_id: uuid.UUID, reason: str, **values: Any) -> str:
    await _set(
        tenant_id,
        build_id,
        status="failed",
        error=reason[:2000],
        finished_at=datetime.now(UTC),
        **values,
    )
    log.info("image.build.failed", tenant_id=str(tenant_id), build_id=str(build_id))
    return "failed"


def _x_registry_auth(auth: RegistryAuth | None, ref: str) -> str | None:
    if auth is None:
        return None
    payload = {
        "username": auth.username,
        "password": auth.password,
        "serveraddress": serveraddress_for(registry_host(ref)),
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def run_verification(
    tenant_id: uuid.UUID,
    build_id: uuid.UUID,
    *,
    registry_client: RegistryClient,
    exec_provider_for: Callable[[str | None], ExecEnvProvider],
    encryptor: Encryptor,
) -> str:
    """Verify, smoke-test and promote one build. Returns the status it ended in.

    Idempotent by state: a build that is no longer verifying or smoke testing is left as
    it is, so a retried job or a second worker does nothing. A smoke test interrupted by
    a dead worker is simply run again -- it has no side effects worth protecting.
    """
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ImageBuildRow, build_id, with_for_update=True)
        if row is None:
            return "missing"
        if row.status not in ("verifying", "smoke_testing"):
            return row.status
        if row.cancel_requested:
            row.status = "cancelled"
            row.finished_at = datetime.now(UTC)
            return "cancelled"
        row.status = "verifying"
        row.attempt += 1
        row.heartbeat_at = datetime.now(UTC)
        target_ref = row.target_ref
        reported = row.reported_digest
        claim = dict(row.harness_claim or {})
        definition = await session.get(ImageDefinitionRow, row.definition_id)
        name = definition.name if definition is not None else ""

    # Re-checked at use: the allowlist or the registries may have changed since import.
    from core.images.namespace import check_image_ref_for_tenant

    try:
        await check_image_ref_for_tenant(tenant_id, target_ref)
    except ImageRefError as exc:
        return await _fail(tenant_id, build_id, str(exc))

    registry = await _registry_for(target_ref)
    auth = await _own_namespace_auth(tenant_id, target_ref, encryptor=encryptor)
    try:
        digest = await registry_client.resolve_digest(
            target_ref, insecure=bool(registry and registry.insecure), auth=auth
        )
    except RegistryError as exc:
        return await _fail(tenant_id, build_id, f"could not verify the image: {exc}")
    if reported and reported != digest:
        return await _fail(
            tenant_id,
            build_id,
            "the builder reported a different digest than the registry holds; "
            "the image was not trusted",
            digest=digest,
        )
    pinned = pinned_ref_for(target_ref, digest)
    await _set(
        tenant_id,
        build_id,
        status="smoke_testing",
        digest=digest,
        pinned_ref=pinned,
        heartbeat_at=datetime.now(UTC),
    )

    from core.exec_engines import get_tenant_engine_key
    from core.harness.registry import get_harness
    from core.images.smoke import parse_smoke, proven_harness, smoke_script

    spec = await get_harness(tenant_id, claim.get("key")) if claim.get("key") else None
    engine_key = await get_tenant_engine_key(tenant_id)
    provider = exec_provider_for(engine_key)
    env_name = f"pyr-img-{build_id.hex[:12]}"
    try:
        result = await provider.run_script(
            env_name,
            pinned,
            smoke_script(spec),
            registry_auth=_x_registry_auth(auth, pinned),
        )
    except ExecEnvUnavailableError as exc:
        return await _fail(
            tenant_id,
            build_id,
            "this organization has no execution engine to check the image on "
            f"({exc}); choose one in the organization settings and check it again",
        )
    except Exception as exc:  # noqa: BLE001 -- an engine fault is this build's failure
        return await _fail(tenant_id, build_id, f"the smoke test could not run: {str(exc)[:500]}")
    finally:
        try:
            await provider.teardown(env_name)
        except Exception:  # noqa: BLE001 -- best effort; the sweep of names catches it
            log.warning("image.smoke.teardown_failed", name=env_name)

    outcome = parse_smoke(result.exit_code, result.output)
    if not outcome.passed:
        return await _fail(tenant_id, build_id, outcome.reason, smoke=outcome.transcript)
    baked = proven_harness(claim.get("key", ""), spec, outcome.harness_output)
    summary = [outcome.git]
    if claim.get("key"):
        summary.append(
            f"harness {claim['key']} {baked['version']} confirmed"
            if baked
            else f"harness {claim['key']} claimed but not confirmed "
            f"({outcome.harness_output or 'no output'}); it will be installed on each run"
        )
    await _set(
        tenant_id,
        build_id,
        status="ready",
        smoke="\n".join(summary) + "\n\n" + outcome.transcript,
        baked_harness=baked or {},
        finished_at=datetime.now(UTC),
    )
    log.info("image.build.ready", tenant_id=str(tenant_id), build_id=str(build_id), name=name)
    try:
        await promote_build(tenant_id, build_id, actor=None)
    except ImageError as exc:
        await _set(tenant_id, build_id, error=f"ready, but not made current: {exc}")
    return "ready"


# ── promotion, cancellation, removal ────────────────────────────────────────────────


async def promote_build(
    tenant_id: uuid.UUID, build_id: uuid.UUID, *, actor: uuid.UUID | None
) -> None:
    """Make a ready build what its definition's runtime runs. The single writer of built
    runtime entries; also how a person rolls back to an earlier ready build."""
    from core.repos.runtimes import InvalidRuntimeError, write_built_runtime

    async with tenant_scope(tenant_id) as session:
        row = await session.get(ImageBuildRow, build_id)
        if row is None:
            raise ImageNotFoundError("no such build")
        if row.status != "ready" or not row.pinned_ref:
            raise ImageError("only a ready, verified build can be made current")
        definition = await session.get(ImageDefinitionRow, row.definition_id)
        if definition is None or definition.archived_at is not None:
            raise ImageNotFoundError("that image was removed")
        name = definition.name
        pinned = row.pinned_ref
        built: dict[str, object] = {
            "build_id": str(row.id),
            "origin": row.origin,
            "digest": row.digest,
        }
        baked = dict(row.baked_harness or {})
    try:
        await write_built_runtime(
            tenant_id, name, image=pinned, built=built, baked_harness=baked or None
        )
    except InvalidRuntimeError as exc:
        raise ImageError(str(exc)) from exc
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            update(ImageDefinitionRow)
            .where(ImageDefinitionRow.tenant_id == tenant_id, ImageDefinitionRow.name == name)
            .values(current_build_id=build_id)
        )
    await _audit(tenant_id, actor, "image.build.promoted", build_id, {"name": name})


async def cancel_build(
    tenant_id: uuid.UUID, name: str, *, actor: uuid.UUID | None
) -> dict[str, Any] | None:
    """Stop the active build of ``name``. One that has not reached anything external stops
    now; one at a builder is cancelled there by the sweep (``core.images.builds``); a
    check in progress finishes its current step and then stops."""
    async with tenant_scope(tenant_id) as session:
        definition = await _definition(session, tenant_id, name)
        row = await session.scalar(
            select(ImageBuildRow)
            .where(
                ImageBuildRow.definition_id == definition.id,
                ImageBuildRow.status.in_(ACTIVE_BUILD_STATUSES),
            )
            .with_for_update()
        )
        if row is None:
            return None
        row.cancel_requested = True
        nothing_started = (row.status == "queued" and not row.external_ref) or (
            row.status == "verifying" and row.job_id is None
        )
        if nothing_started:
            row.status = "cancelled"
            row.finished_at = datetime.now(UTC)
        build_id = row.id
    await _audit(tenant_id, actor, "image.build.cancelled", build_id, {"name": name})
    return await get_build(tenant_id, build_id)


async def recheck_image(
    tenant_id: uuid.UUID, name: str, *, requested_by: uuid.UUID | None, queue: JobQueue | None
) -> dict[str, Any]:
    """Check an imported image again -- after a failure that was the engine's or the
    registry's fault, or after the operator changed something."""
    async with tenant_scope(tenant_id) as session:
        definition = await _definition(session, tenant_id, name)
        if definition.origin != "imported":
            raise ImageError("only an imported image can be checked again; rebuild it instead")
        last = await session.scalar(
            select(ImageBuildRow)
            .where(ImageBuildRow.definition_id == definition.id)
            .order_by(ImageBuildRow.created_at.desc())
            .limit(1)
        )
        if last is None:
            raise ImageError("this image has nothing to check")
        ref, dockerfile, claim = last.target_ref, last.dockerfile, dict(last.harness_claim)
    return await import_image(
        tenant_id,
        name=name,
        image=ref,
        dockerfile=dockerfile,
        harness_claim=claim,
        requested_by=requested_by,
        queue=queue,
    )


async def remove_image(tenant_id: uuid.UUID, name: str, *, actor: uuid.UUID | None) -> None:
    """Stop offering an image: its runtime goes, its history stays. Repos that picked it
    are refused at their next delegation with "unknown runtime" rather than silently
    moved onto another image."""
    from core.repos.runtimes import drop_built_runtime

    async with tenant_scope(tenant_id) as session:
        definition = await _definition(session, tenant_id, name)
        definition.archived_at = datetime.now(UTC)
        definition.current_build_id = None
        definition_id = definition.id
        await session.execute(
            update(ImageBuildRow)
            .where(
                ImageBuildRow.definition_id == definition_id,
                ImageBuildRow.status.in_(ACTIVE_BUILD_STATUSES),
            )
            .values(status="cancelled", cancel_requested=True, finished_at=datetime.now(UTC))
        )
    await drop_built_runtime(tenant_id, name)
    await _audit(tenant_id, actor, "image.removed", definition_id, {"name": name})


# ── the sweep ───────────────────────────────────────────────────────────────────────


async def sweep_image_builds(queue: JobQueue) -> tuple[int, int]:
    """(enqueued, failed): start checks nobody started, and fail ones nobody finished.

    Cross-tenant the way ``core.previews.service.due_for_reaping`` is: enumerate tenants
    on the unscoped ``tenant`` table, then a normal ``tenant_scope`` per tenant.
    """
    from core.tenancy.models import Tenant

    async with unscoped_session() as session:
        tenant_ids = list(await session.scalars(select(Tenant.id)))
    now = datetime.now(UTC)
    enqueued = failed = 0
    for tenant_id in tenant_ids:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.scalars(
                    select(ImageBuildRow).where(
                        ImageBuildRow.tenant_id == tenant_id,
                        ImageBuildRow.status.in_(("verifying", "smoke_testing")),
                    )
                )
            ).all()
            pending = [r.id for r in rows if r.job_id is None and not r.cancel_requested]
            stale = [
                r.id
                for r in rows
                if r.job_id is not None and (r.heartbeat_at or r.updated_at) < now - _STALE_AFTER
            ]
        for build_id in pending:
            await _enqueue(queue, tenant_id, build_id)
            enqueued += 1
        for build_id in stale:
            await _fail(tenant_id, build_id, "the check stopped responding; check the image again")
            failed += 1
    return enqueued, failed


async def _requeue_for_verification(
    queue: JobQueue, tenant_id: uuid.UUID, build_id: uuid.UUID
) -> None:
    """A builder finished: the same check an import gets, from the top."""
    await _set(tenant_id, build_id, status="verifying", job_id=None)
    await _enqueue(queue, tenant_id, build_id)
