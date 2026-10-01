"""The building half of an image's life: a Dockerfile to a builder, and back.

    queued -> submitted -> building -> verifying -> smoke_testing -> ready

The verifying and smoke-testing steps are ``core.images.service``'s, the same ones an
imported image goes through: whatever a builder says it pushed, the registry is asked for
the digest and the result is smoke-tested before anything runs in it.

**Where an image goes is never the tenant's choice.** ``target_ref`` is computed from
the builder's registry and the tenant's own namespace there, and tagged with the recipe's
content hash plus the build id, so a build can never overwrite another's tag.

**One submission per build.** ``queued -> submitted`` is claimed under the row lock and
committed *before* the builder is called; a worker that dies mid-call leaves a submitted
build with no external id, which the sweep fails rather than submitting twice.

**Content addressing.** The hash covers the Dockerfile, the baked harness and the
platform. Asking to build the same recipe on the same builder again re-uses the stored
digest -- the hash names the recipe, the digest names the bytes, and base images move, so
"Rebuild anyway" exists to get new bytes for the same recipe.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.images.models import ACTIVE_BUILD_STATUSES, ImageBuildRow, ImageDefinitionRow
from core.images.service import (
    ImageError,
    ImageNotFoundError,
    _audit,
    _check_name,
    _definition,
    _fail,
    _requeue_for_verification,
    _set,
    get_build,
    promote_build,
)
from core.ports.image_builder import BuilderError, BuildSpec, ImageBuilder
from core.ports.job_queue import JobQueue
from core.repos.image_ref import ImageRefError
from core.tenancy.scope import tenant_scope, unscoped_session

log = structlog.get_logger()

ADVANCE_JOB_KIND = "advance_image_build"
PLATFORM = "linux/amd64"
# A build marked submitted with no external id: the worker died between claiming the
# submission and hearing back. Long enough that a slow builder's answer is not cut off.
_LOST_SUBMISSION_AFTER = timedelta(minutes=10)

BuilderFor = Callable[[str], Awaitable[ImageBuilder]]


# ── content ─────────────────────────────────────────────────────────────────────────


def harness_layer(spec: dict[str, Any] | None) -> str:
    """The lines appended to install a harness: the operator's own setup commands for it,
    added by code -- never by a person editing the file or a model proposing one."""
    if not spec:
        return ""
    commands = [str(c) for c in spec.get("setup_cmds") or [] if str(c).strip()]
    if not commands:
        return ""
    key = str(spec.get("key") or "harness")
    lines = [f"# --- added by Pyrrhula: the {key} harness ---"]
    lines += [f"RUN {c}" for c in commands]
    return "\n".join(lines) + "\n"


def final_dockerfile(dockerfile: str, spec: dict[str, Any] | None) -> str:
    text = dockerfile if dockerfile.endswith("\n") else dockerfile + "\n"
    layer = harness_layer(spec)
    return text + ("\n" + layer if layer else "")


def content_hash(dockerfile: str, baked: dict[str, str] | None, nonce: str = "") -> str:
    canonical = json.dumps(
        {
            "schema": 1,
            "dockerfile": dockerfile,
            "baked_harness": baked or None,
            "platform": PLATFORM,
            "nonce": nonce,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── definitions ─────────────────────────────────────────────────────────────────────


async def save_definition(
    tenant_id: uuid.UUID,
    name: str,
    *,
    dockerfile: str,
    harness_key: str,
    actor: uuid.UUID | None,
) -> dict[str, Any]:
    """Create or update an image this organization builds. Saving builds nothing."""
    from core.images.dockerfile import MAX_BYTES

    name = _check_name(name)
    if len(dockerfile.encode()) > MAX_BYTES:
        raise ImageError(f"the Dockerfile is larger than {MAX_BYTES // 1024} KiB")
    harness_key = (harness_key or "").strip()
    if harness_key:
        from core.harness.registry import get_harness

        if await get_harness(tenant_id, harness_key) is None:
            raise ImageError(f"this organization has no harness {harness_key!r}")
    from core.repos.runtimes import is_built, resolved_runtimes

    existing_runtime = (await resolved_runtimes(tenant_id)).get(name)
    if existing_runtime is not None and not is_built(existing_runtime):
        raise ImageError(
            f"this organization already has a runtime called {name!r}; give the image another name"
        )
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(ImageDefinitionRow).where(
                ImageDefinitionRow.tenant_id == tenant_id, ImageDefinitionRow.name == name
            )
        )
        if row is None:
            row = ImageDefinitionRow(
                tenant_id=tenant_id, name=name, origin="built", created_by=actor
            )
            session.add(row)
        elif row.origin == "imported" and row.archived_at is None:
            raise ImageError(f"{name!r} is an imported image; build under another name")
        row.origin = "built"
        row.archived_at = None
        row.dockerfile = dockerfile
        row.harness_key = harness_key
        row.updated_by = actor
        await session.flush()
        definition_id = row.id
    await _audit(tenant_id, actor, "image.definition.saved", definition_id, {"name": name})
    return {"id": str(definition_id), "name": name}


async def check_dockerfile(tenant_id: uuid.UUID, dockerfile: str) -> dict[str, list[str]]:
    """The validator plus the checks that need the database: every base image is held
    to the namespace and the runtime-image allowlist, exactly like a runtime image."""
    from core.images.dockerfile import validate_dockerfile
    from core.images.namespace import check_image_ref_for_tenant

    report = validate_dockerfile(dockerfile)
    for ref in report.from_refs:
        try:
            await check_image_ref_for_tenant(tenant_id, ref)
        except ImageRefError as exc:
            report.errors.append(f"FROM {ref}: {exc}")
    return {"errors": report.errors, "warnings": report.warnings}


# ── requesting a build ──────────────────────────────────────────────────────────────


async def request_build(
    tenant_id: uuid.UUID,
    name: str,
    *,
    builder_key: str,
    requested_by: uuid.UUID | None,
    rebuild: bool = False,
    queue: JobQueue | None = None,
) -> dict[str, Any]:
    from core.harness.registry import get_harness, harness_fingerprint
    from core.images.builders import builders_for_tenant
    from core.images.namespace import repository_for
    from core.images.policy import get_image_build_limits
    from core.images.registries import get_registry

    builder = next((b for b in await builders_for_tenant(tenant_id) if b.key == builder_key), None)
    if builder is None:
        raise ImageError(f"builder {builder_key!r} is not available to this organization")

    async with tenant_scope(tenant_id) as session:
        definition = await _definition(session, tenant_id, name)
        if definition.origin != "built":
            raise ImageError("an imported image is checked, not built; check it again instead")
        dockerfile, harness_key, definition_id = (
            definition.dockerfile,
            definition.harness_key,
            definition.id,
        )

    spec = None
    if harness_key:
        spec = await get_harness(tenant_id, harness_key)
        if spec is None:
            raise ImageError(f"the harness {harness_key!r} is no longer available here")
        spec = {"key": harness_key, **spec}
    final = final_dockerfile(dockerfile, spec)
    checked = await check_dockerfile(tenant_id, final)
    if checked["errors"]:
        raise ImageError("the Dockerfile cannot be built: " + "; ".join(checked["errors"]))

    claim: dict[str, str] = {}
    baked_input: dict[str, str] | None = None
    if spec is not None:
        claim = {"key": harness_key, "version": str(spec.get("version") or "")}
        baked_input = {**claim, "fingerprint": harness_fingerprint(spec)}
    nonce = uuid.uuid4().hex if rebuild else ""
    digest_key = content_hash(final, baked_input, nonce)

    limits = await get_image_build_limits()
    registry = await get_registry(builder.registry_key)
    build_id = uuid.uuid4()
    target_ref = (
        f"{repository_for(registry.namespace(), tenant_id, name)}"
        f":{digest_key[:20]}-{build_id.hex[:8]}"
    )
    try:
        async with tenant_scope(tenant_id) as session:
            reuse_id: uuid.UUID | None = None
            if not rebuild:
                same = await session.scalar(
                    select(ImageBuildRow)
                    .where(
                        ImageBuildRow.definition_id == definition_id,
                        ImageBuildRow.content_hash == digest_key,
                        ImageBuildRow.builder_key == builder.key,
                        ImageBuildRow.registry_key == builder.registry_key,
                        ImageBuildRow.status == "ready",
                    )
                    .order_by(ImageBuildRow.created_at.desc())
                    .limit(1)
                )
                reuse_id = same.id if same is not None else None
            if reuse_id is None:
                active = await session.scalar(
                    select(func.count())
                    .select_from(ImageBuildRow)
                    .where(
                        ImageBuildRow.tenant_id == tenant_id,
                        ImageBuildRow.origin == "built",
                        ImageBuildRow.status.in_(ACTIVE_BUILD_STATUSES),
                    )
                )
                if (active or 0) >= limits["max_concurrent_per_tenant"]:
                    raise ImageError(
                        "this organization already has as many builds running as the "
                        "deployment allows; wait for one to finish"
                    )
                today = await session.scalar(
                    select(func.count())
                    .select_from(ImageBuildRow)
                    .where(
                        ImageBuildRow.tenant_id == tenant_id,
                        ImageBuildRow.origin == "built",
                        ImageBuildRow.created_at > datetime.now(UTC) - timedelta(days=1),
                    )
                )
                if (today or 0) >= limits["max_per_day_per_tenant"]:
                    raise ImageError(
                        f"this organization has used its {limits['max_per_day_per_tenant']} "
                        "builds for the last 24 hours"
                    )
                session.add(
                    ImageBuildRow(
                        id=build_id,
                        tenant_id=tenant_id,
                        definition_id=definition_id,
                        origin="built",
                        status="queued",
                        builder_key=builder.key,
                        registry_key=builder.registry_key,
                        dockerfile=final,
                        content_hash=digest_key,
                        target_ref=target_ref,
                        harness_claim=claim,
                        requested_by=requested_by,
                    )
                )
                await session.flush()
    except IntegrityError as exc:
        raise ImageError(f"{name!r} is already building; wait for it or cancel it") from exc

    if reuse_id is not None:
        await promote_build(tenant_id, reuse_id, actor=requested_by)
        return {**(await get_build(tenant_id, reuse_id)), "reused": True}

    await _audit(
        tenant_id,
        requested_by,
        "image.build.requested",
        build_id,
        {"name": name, "builder": builder.key, "target_ref": target_ref},
    )
    if queue is not None:
        await queue.enqueue(
            tenant_id, ADVANCE_JOB_KIND, {"tenant_id": str(tenant_id), "build_id": str(build_id)}
        )
    return {**(await get_build(tenant_id, build_id)), "reused": False}


# ── following a build ───────────────────────────────────────────────────────────────


async def advance_build(
    tenant_id: uuid.UUID,
    build_id: uuid.UUID,
    *,
    builder_for: BuilderFor,
    queue: JobQueue,
) -> str:
    """One step: submit a queued build, or poll a submitted one, or cancel one a person
    asked to stop. Returns the status it left the build in. Safe to call from the job and
    the sweep at once -- every transition is claimed under the row lock."""
    from core.images.policy import get_image_build_limits

    limits = await get_image_build_limits()
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ImageBuildRow, build_id, with_for_update=True)
        if row is None:
            return "missing"
        status = row.status
        if status not in ("queued", "submitted", "building"):
            return status
        builder_key = row.builder_key or ""
        external_ref = row.external_ref
        cancel = row.cancel_requested
        age = datetime.now(UTC) - row.created_at
        if status == "queued" and not cancel:
            # Claim the submission before making it: never twice.
            row.status = "submitted"
            row.heartbeat_at = datetime.now(UTC)
        spec = BuildSpec(
            build_id=str(row.id),
            dockerfile=row.dockerfile,
            target_ref=row.target_ref,
            labels={
                "pyrrhula.build": str(row.id),
                "pyrrhula.content-hash": row.content_hash,
            },
        )

    if status == "queued" and cancel:
        await _set(tenant_id, build_id, status="cancelled", finished_at=datetime.now(UTC))
        return "cancelled"

    try:
        builder = await builder_for(builder_key)
    except Exception as exc:  # noqa: BLE001 -- a builder that vanished fails this build
        return await _fail(tenant_id, build_id, f"the builder is not available: {exc}")

    if status == "queued":
        try:
            submitted = await builder.submit(spec)
        except BuilderError as exc:
            return await _fail(tenant_id, build_id, f"the builder refused the build: {exc}")
        await _set(
            tenant_id,
            build_id,
            external_ref=submitted.external_ref,
            external_url=submitted.external_url,
            heartbeat_at=datetime.now(UTC),
        )
        log.info("image.build.submitted", tenant_id=str(tenant_id), build_id=str(build_id))
        return "submitted"

    if not external_ref:
        if age > _LOST_SUBMISSION_AFTER:
            return await _fail(
                tenant_id,
                build_id,
                "the submission was interrupted; build again",
            )
        return status

    if cancel or age > timedelta(seconds=limits["timeout_seconds"]):
        try:
            await builder.cancel(external_ref)
        except BuilderError:
            log.warning("image.build.cancel_failed", build_id=str(build_id))
        if cancel:
            await _set(tenant_id, build_id, status="cancelled", finished_at=datetime.now(UTC))
            return "cancelled"
        return await _fail(
            tenant_id,
            build_id,
            f"the build took longer than {limits['timeout_seconds'] // 60} minutes",
        )

    try:
        progress = await builder.poll(external_ref)
    except BuilderError as exc:
        # Transient until the wall clock says otherwise: a builder that is briefly down
        # must not fail a build that is still running there.
        await _set(tenant_id, build_id, error=f"last check: {exc}"[:1000])
        return status
    values: dict[str, Any] = {"heartbeat_at": datetime.now(UTC), "error": ""}
    if progress.log_tail:
        values["log_tail"] = progress.log_tail
    if progress.external_url:
        values["external_url"] = progress.external_url
    if progress.state in ("queued", "building"):
        await _set(
            tenant_id,
            build_id,
            status="building" if progress.state == "building" else "submitted",
            **values,
        )
        return "building" if progress.state == "building" else "submitted"
    if progress.state == "cancelled":
        await _set(tenant_id, build_id, status="cancelled", finished_at=datetime.now(UTC), **values)
        return "cancelled"
    if progress.state == "failed":
        values.pop("error")
        return await _fail(tenant_id, build_id, progress.error or "the build failed", **values)
    # succeeded: the builder's word, which the check that follows does not take.
    await _set(tenant_id, build_id, reported_digest=progress.digest, **values)
    await _requeue_for_verification(queue, tenant_id, build_id)
    log.info("image.build.built", tenant_id=str(tenant_id), build_id=str(build_id))
    return "verifying"


async def sweep_builder_builds(queue: JobQueue, builder_for: BuilderFor) -> int:
    """Advance every build waiting on a builder, across tenants. Returns how many moved."""
    from core.tenancy.models import Tenant

    async with unscoped_session() as session:
        tenant_ids = list(await session.scalars(select(Tenant.id)))
    moved = 0
    for tenant_id in tenant_ids:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(ImageBuildRow.id, ImageBuildRow.status).where(
                        ImageBuildRow.tenant_id == tenant_id,
                        ImageBuildRow.status.in_(("queued", "submitted", "building")),
                    )
                )
            ).all()
        for build_id, before in rows:
            try:
                after = await advance_build(
                    tenant_id, build_id, builder_for=builder_for, queue=queue
                )
            except Exception as exc:  # noqa: BLE001 -- one build must not stop the sweep
                log.warning("image.build.advance_failed", build_id=str(build_id), error=str(exc))
                continue
            moved += int(after != before)
    return moved


__all__ = [
    "ADVANCE_JOB_KIND",
    "ImageError",
    "ImageNotFoundError",
    "advance_build",
    "check_dockerfile",
    "content_hash",
    "final_dockerfile",
    "request_build",
    "save_definition",
    "sweep_builder_builds",
]
