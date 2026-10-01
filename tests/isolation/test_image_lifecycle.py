"""Images: tenant isolation, the check every image passes, and the `.pyr` round trip.

What has to hold:

- ``image_definition`` and ``image_build`` are invisible across tenants (rule 4).
- Nothing becomes a runtime until the registry confirmed the digest and a smoke test on
  the tenant's own engine passed; the runtime then runs the digest, never a tag.
- A bundle's claim that a harness is baked in is believed only when the smoke test
  printed the harness's version.
- A `.pyr` carries the exact reference; an image the importing deployment refuses does
  not fail the import, it is reported with the reason.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.images.models import ImageBuildRow
from core.images.policy import set_runtime_image_allowlist
from core.images.service import (
    ImageError,
    import_image,
    list_images,
    promote_build,
    remove_image,
    run_verification,
    sweep_image_builds,
)
from core.portability.bundle import open_bundle
from core.portability.export import ExportOptions, export_workspace
from core.portability.import_ import import_bundle
from core.ports.exec_env import ExecEnvUnavailableError, ExecResult
from core.ports.image_registry import RegistryError
from core.repos.runtimes import (
    InvalidRuntimeError,
    get_runtime,
    register_runtime,
    remove_runtime,
)
from core.tenancy.models import Principal, Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "b" * 64
REF = f"ghcr.io/example/godot-node@{DIGEST}"
_PASS = "pyr-smoke:begin\npyr-smoke:git=git version 2.39.5\npyr-smoke:end\n"
_PASS_WITH_HARNESS = (
    "pyr-smoke:begin\npyr-smoke:git=git version 2.39.5\npyr-smoke:harness=1.18.33\npyr-smoke:end\n"
)


@dataclass
class _Registry:
    digest: str = DIGEST
    error: str | None = None
    asked: list[str] = field(default_factory=list)

    async def probe(self, host: str, *, insecure: bool, auth: Any) -> Any:
        raise NotImplementedError

    async def resolve_digest(self, ref: str, *, insecure: bool, auth: Any) -> str:
        self.asked.append(ref)
        if self.error:
            raise RegistryError(self.error)
        return self.digest


@dataclass
class _Engine:
    output: str = _PASS
    exit_code: int = 0
    unavailable: bool = False
    ran: list[tuple[str, str]] = field(default_factory=list)
    torn_down: list[str] = field(default_factory=list)

    async def run_script(
        self, name: str, image: str, script: str, *, registry_auth: str | None = None
    ) -> ExecResult:
        if self.unavailable:
            raise ExecEnvUnavailableError("none")
        self.ran.append((name, image))
        return ExecResult(self.exit_code, self.output)

    async def teardown(self, env_ref: str) -> None:
        self.torn_down.append(env_ref)


async def _tenant() -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"img-{uuid.uuid4().hex[:8]}")
    return tenant_id, owner_id


async def _verify(tenant_id: uuid.UUID, build_id: str, registry: _Registry, engine: _Engine) -> str:
    return await run_verification(
        tenant_id,
        uuid.UUID(build_id),
        registry_client=registry,
        exec_provider_for=lambda _key: engine,  # type: ignore[arg-type,return-value]
        encryptor=IdentityEncryptor(),
    )


# ── isolation ───────────────────────────────────────────────────────────────────────


async def test_images_are_invisible_to_another_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await import_image(tenant_a, name="godot-node", image=REF, requested_by=None)
    assert [i["name"] for i in await list_images(tenant_a)] == ["godot-node"]
    assert await list_images(tenant_b) == []
    async with tenant_scope(tenant_b) as session:
        for table in ("image_definition", "image_build"):
            leaked = await session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert leaked == 0, table
    async with tenant_scope(tenant_b) as session:
        changed = await session.execute(text("UPDATE image_build SET status = 'ready'"))
        assert changed.rowcount == 0  # type: ignore[attr-defined]


# ── the check ───────────────────────────────────────────────────────────────────────


async def test_an_imported_image_must_be_pinned_by_digest(db_available: None) -> None:
    tenant_id, _ = await _tenant()
    with pytest.raises(ImageError, match="digest"):
        await import_image(
            tenant_id, name="godot-node", image="ghcr.io/x/godot:4", requested_by=None
        )


async def test_a_checked_image_becomes_a_runtime_that_runs_its_digest(
    db_available: None,
) -> None:
    tenant_id, owner = await _tenant()
    build = await import_image(
        tenant_id,
        name="godot-node",
        image="ghcr.io/example/godot-node:4.3@" + DIGEST,
        requested_by=owner,
    )
    assert await get_runtime(tenant_id, "godot-node") is None, "not before the check"
    registry, engine = _Registry(), _Engine()
    assert await _verify(tenant_id, build["id"], registry, engine) == "ready"

    runtime = await get_runtime(tenant_id, "godot-node")
    assert runtime is not None
    assert runtime["image"] == REF, "the tag is dropped; the digest is what runs"
    assert engine.ran == [(f"pyr-img-{uuid.UUID(build['id']).hex[:12]}", REF)]
    assert engine.torn_down, "the smoke environment does not outlive the check"
    images = await list_images(tenant_id)
    assert images[0]["current"]["id"] == build["id"]


async def test_a_failed_check_leaves_no_runtime(db_available: None) -> None:
    tenant_id, _ = await _tenant()
    cases = [
        (_Registry(error="manifest unknown"), _Engine(), "could not verify"),
        (_Registry(), _Engine(output="pyr-smoke:begin\npyr-smoke:git=\npyr-smoke:end"), "git"),
        (_Registry(), _Engine(unavailable=True), "execution engine"),
    ]
    for i, (registry, engine, reason) in enumerate(cases):
        name = f"img{i}"
        build = await import_image(tenant_id, name=name, image=REF, requested_by=None)
        assert await _verify(tenant_id, build["id"], registry, engine) == "failed"
        assert await get_runtime(tenant_id, name) is None
        latest = (await list_images(tenant_id))[i]["latest"]
        assert reason in latest["error"], latest["error"]


async def test_a_harness_claim_needs_the_smoke_test_to_print_its_version(
    db_available: None,
) -> None:
    tenant_id, _ = await _tenant()
    claim = {"key": "opencode", "version": "1.18.33"}
    unproven = await import_image(
        tenant_id, name="claims", image=REF, harness_claim=claim, requested_by=None
    )
    await _verify(tenant_id, unproven["id"], _Registry(), _Engine(output=_PASS))
    runtime = await get_runtime(tenant_id, "claims")
    assert runtime is not None and "baked_harness" not in runtime

    proven = await import_image(
        tenant_id, name="proves", image=REF, harness_claim=claim, requested_by=None
    )
    await _verify(tenant_id, proven["id"], _Registry(), _Engine(output=_PASS_WITH_HARNESS))
    runtime = await get_runtime(tenant_id, "proves")
    assert runtime is not None
    assert runtime["baked_harness"]["key"] == "opencode"  # type: ignore[index]


async def test_a_built_runtime_is_changed_only_through_images(db_available: None) -> None:
    tenant_id, owner = await _tenant()
    build = await import_image(tenant_id, name="godot-node", image=REF, requested_by=owner)
    await _verify(tenant_id, build["id"], _Registry(), _Engine())
    with pytest.raises(InvalidRuntimeError, match="Images"):
        await register_runtime(tenant_id, "godot-node", "python:3.12")
    with pytest.raises(InvalidRuntimeError, match="Images"):
        await remove_runtime(tenant_id, "godot-node")
    await remove_image(tenant_id, "godot-node", actor=owner)
    assert await get_runtime(tenant_id, "godot-node") is None


async def test_an_image_cannot_take_a_hand_registered_runtimes_name(db_available: None) -> None:
    tenant_id, _ = await _tenant()
    await register_runtime(tenant_id, "rust", "docker.io/library/rust:1")
    with pytest.raises(ImageError, match="already has a runtime"):
        await import_image(tenant_id, name="rust", image=REF, requested_by=None)
    with pytest.raises(ImageError, match="built-in"):
        await import_image(tenant_id, name="debian", image=REF, requested_by=None)


async def test_rollback_makes_an_earlier_ready_build_current(db_available: None) -> None:
    tenant_id, owner = await _tenant()
    first = await import_image(tenant_id, name="godot-node", image=REF, requested_by=owner)
    await _verify(tenant_id, first["id"], _Registry(), _Engine())
    other = "sha256:" + "c" * 64
    second = await import_image(
        tenant_id,
        name="godot-node",
        image=f"ghcr.io/example/godot-node@{other}",
        requested_by=owner,
    )
    await _verify(tenant_id, second["id"], _Registry(digest=other), _Engine())
    assert (await get_runtime(tenant_id, "godot-node"))["image"].endswith(other)  # type: ignore[index,union-attr]
    await promote_build(tenant_id, uuid.UUID(first["id"]), actor=owner)
    assert (await get_runtime(tenant_id, "godot-node"))["image"] == REF  # type: ignore[index]


async def test_the_sweep_starts_a_check_nobody_started(db_available: None) -> None:
    tenant_id, _ = await _tenant()
    build = await import_image(tenant_id, name="godot-node", image=REF, requested_by=None)

    class _Queue:
        def __init__(self) -> None:
            self.jobs: list[tuple[str, dict[str, Any]]] = []

        async def enqueue(self, tenant: uuid.UUID, kind: str, payload: dict[str, Any]) -> uuid.UUID:
            self.jobs.append((kind, payload))
            return uuid.uuid4()

    queue = _Queue()
    await sweep_image_builds(queue)  # type: ignore[arg-type]
    mine = [p for k, p in queue.jobs if p.get("build_id") == build["id"]]
    assert mine == [{"tenant_id": str(tenant_id), "build_id": build["id"]}], (
        "the job carries ids only -- no reference, no credential"
    )
    await sweep_image_builds(queue)  # type: ignore[arg-type]
    assert len([p for k, p in queue.jobs if p.get("build_id") == build["id"]]) == 1


# ── .pyr ────────────────────────────────────────────────────────────────────────────


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _principal(tenant_id: uuid.UUID, principal_id: uuid.UUID) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = await session.get(Principal, principal_id)
        assert principal is not None
        session.expunge(principal)
        return principal


async def _export_images(tenant_id: uuid.UUID, owner: uuid.UUID) -> bytes:
    result = await export_workspace(
        await _principal(tenant_id, owner),
        tenant_id,
        await _workspace_of(tenant_id),
        options=ExportOptions(sections=frozenset({"images"})),
        encryptor=IdentityEncryptor(),
        permission_service=RolePermissionService(),
    )
    return result.data


async def _import(data: bytes, tenant_id: uuid.UUID, principal_id: uuid.UUID) -> Any:
    return await import_bundle(
        data,
        tenant_id,
        await _workspace_of(tenant_id),
        bundle_ref="images",
        encryptor=IdentityEncryptor(),
        importing_principal_id=principal_id,
        permission_service=RolePermissionService(),
        moderation_provider=AllowAllModerationProvider(),
        sections=frozenset({"images"}),
    )


async def test_a_bundle_carries_the_exact_image_and_the_importer_checks_it_again(
    db_available: None,
) -> None:
    source, source_owner = await _tenant()
    build = await import_image(
        source,
        name="godot-node",
        image=REF,
        dockerfile="FROM debian:bookworm\nRUN apt-get install -y git\n",
        harness_claim={"key": "opencode", "version": "1.18.33"},
        requested_by=source_owner,
    )
    await _verify(source, build["id"], _Registry(), _Engine(output=_PASS_WITH_HARNESS))
    data = await _export_images(source, source_owner)

    files = open_bundle(data).files
    import json

    carried = json.loads(files["images/godot-node.json"])
    assert carried["image"] == REF
    assert carried["harness_claim"] == {"key": "opencode", "version": "1.18.33"}
    assert "FROM debian" in carried["dockerfile"]
    assert not any(k in json.dumps(carried) for k in ("credential", "builder", "password"))

    target, target_owner = await _tenant()
    report = await _import(data, target, target_owner)
    assert "image:godot-node (being checked; usable once it passes)" in report.imported
    assert await get_runtime(target, "godot-node") is None, "not usable before its check"
    async with tenant_scope(target) as session:
        row = await session.scalar(select(ImageBuildRow).where(ImageBuildRow.tenant_id == target))
        assert row is not None and row.status == "verifying" and row.baked_harness == {}
        new_build = str(row.id)
    # The claim travelled; the importer's own smoke test decides whether it is believed.
    await _verify(target, new_build, _Registry(), _Engine(output=_PASS))
    runtime = await get_runtime(target, "godot-node")
    assert runtime is not None and runtime["image"] == REF and "baked_harness" not in runtime


async def test_a_refused_image_is_reported_and_the_rest_imports(db_available: None) -> None:
    source, source_owner = await _tenant()
    build = await import_image(
        source, name="godot-node", image=REF, dockerfile="FROM x:1\n", requested_by=source_owner
    )
    await _verify(source, build["id"], _Registry(), _Engine())
    data = await _export_images(source, source_owner)

    target, target_owner = await _tenant()
    await set_runtime_image_allowlist(["docker.io/library/"])
    try:
        report = await _import(data, target, target_owner)
    finally:
        await set_runtime_image_allowlist([])
    skipped = [s for s in report.skipped if s.startswith("image:godot-node")]
    assert skipped and "Dockerfile is in the bundle" in skipped[0], report.skipped
    assert await list_images(target) == []
