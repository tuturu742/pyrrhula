"""Building an image on an external builder, followed to a verified runtime.

What has to hold:

- the image goes where the platform says -- the builder's registry, inside the tenant's
  own namespace -- never where a tenant or a Dockerfile says;
- a build is submitted once, even when the job and the sweep advance it together, and a
  worker that died mid-submission fails the build rather than submitting it twice;
- the builder's reported digest is never trusted alone: the registry is asked, and a
  mismatch fails the build;
- the operator's choices bind: which organizations may use a builder, and how many
  builds an organization may run;
- a Dockerfile the validator refuses never reaches a builder.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update

from adapters.encryptor.identity import IdentityEncryptor
from core.images.builders import create_builder, delete_builder, set_builder_credential
from core.images.builds import advance_build, request_build, save_definition
from core.images.models import ImageBuildRow
from core.images.registries import create_registry, delete_registry
from core.images.service import ImageError, cancel_build, run_verification
from core.ports.exec_env import ExecResult
from core.ports.image_builder import BuildSpec, Progress, Submitted
from core.repos.runtimes import get_runtime
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "f" * 64
DOCKERFILE = "FROM debian:bookworm\nRUN apt-get update && apt-get install -y git\n"


@dataclass
class FakeBuilder:
    state: str = "building"
    digest: str = DIGEST
    submitted: list[BuildSpec] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    mode: str = "poll"

    async def submit(self, spec: BuildSpec) -> Submitted:
        self.submitted.append(spec)
        return Submitted(f"run-{len(self.submitted)}", "https://ci.example/run")

    async def poll(self, external_ref: str) -> Progress:
        return Progress(self.state, digest=self.digest if self.state == "succeeded" else "")  # type: ignore[arg-type]

    async def cancel(self, external_ref: str) -> None:
        self.cancelled.append(external_ref)

    async def probe(self) -> Any:
        raise NotImplementedError


@dataclass
class FakeQueue:
    jobs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def enqueue(self, tenant_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> uuid.UUID:
        self.jobs.append((kind, payload))
        return uuid.uuid4()


@dataclass
class Setup:
    tenant_id: uuid.UUID
    owner: uuid.UUID
    builder_key: str
    host: str
    fake: FakeBuilder
    queue: FakeQueue

    async def builder_for(self, key: str) -> Any:
        assert key == self.builder_key
        return self.fake

    async def advance(self, build_id: str) -> str:
        return await advance_build(
            self.tenant_id,
            uuid.UUID(build_id),
            builder_for=self.builder_for,
            queue=self.queue,  # type: ignore[arg-type]
        )


@pytest_asyncio.fixture
async def setup(db_available: None) -> AsyncIterator[Setup]:
    suffix = uuid.uuid4().hex[:8]
    tenant_id, owner, _ = await seed_dev_tenant(slug=f"build-{suffix}")
    host = f"reg-{suffix}.example"
    await create_registry(f"r{suffix}", {"pull_host": host, "path_prefix": "pyr"})
    await create_builder(
        f"b{suffix}",
        "webhook",
        {
            "registry_key": f"r{suffix}",
            "config": {
                "submit_url": "https://builds.example/submit",
                "status_url": "https://builds.example/status",
            },
        },
    )
    await set_builder_credential(
        f"b{suffix}", {"signing_secret": "s" * 32}, encryptor=IdentityEncryptor()
    )
    yield Setup(tenant_id, owner, f"b{suffix}", host, FakeBuilder(), FakeQueue())
    await delete_builder(f"b{suffix}")
    await delete_registry(f"r{suffix}")


async def _define(
    s: Setup, name: str = "godot-node", dockerfile: str = DOCKERFILE, harness: str = ""
) -> None:
    await save_definition(
        s.tenant_id, name, dockerfile=dockerfile, harness_key=harness, actor=s.owner
    )


async def _request(s: Setup, name: str = "godot-node", rebuild: bool = False) -> dict[str, Any]:
    return await request_build(
        s.tenant_id,
        name,
        builder_key=s.builder_key,
        requested_by=s.owner,
        rebuild=rebuild,
        queue=s.queue,  # type: ignore[arg-type]
    )


class _Registry:
    def __init__(self, digest: str = DIGEST) -> None:
        self.digest = digest

    async def resolve_digest(self, ref: str, *, insecure: bool, auth: Any) -> str:
        return self.digest


class _Engine:
    async def run_script(
        self, name: str, image: str, script: str, *, registry_auth: Any = None
    ) -> ExecResult:
        return ExecResult(0, "pyr-smoke:begin\npyr-smoke:git=git version 2.39\npyr-smoke:end\n")

    async def teardown(self, env_ref: str) -> None:
        return None


async def _verify(s: Setup, build_id: str, registry_digest: str = DIGEST) -> str:
    return await run_verification(
        s.tenant_id,
        uuid.UUID(build_id),
        registry_client=_Registry(registry_digest),  # type: ignore[arg-type]
        exec_provider_for=lambda _k: _Engine(),  # type: ignore[arg-type,return-value]
        encryptor=IdentityEncryptor(),
    )


async def test_a_build_goes_from_dockerfile_to_a_verified_runtime(setup: Setup) -> None:
    s = setup
    await _define(s)
    build = await _request(s)
    assert build["status"] == "queued"
    assert build["target_ref"].startswith(f"{s.host}/pyr/t{s.tenant_id.hex}/godot-node:")
    assert [k for k, _ in s.queue.jobs] == ["advance_image_build"]

    assert await s.advance(build["id"]) == "submitted"
    assert s.fake.submitted[0].target_ref == build["target_ref"]
    assert await s.advance(build["id"]) == "building"
    s.fake.state = "succeeded"
    assert await s.advance(build["id"]) == "verifying"
    kinds = [k for k, _ in s.queue.jobs]
    assert kinds[-1] == "verify_image_build"
    assert s.queue.jobs[-1][1] == {"tenant_id": str(s.tenant_id), "build_id": build["id"]}

    assert await _verify(s, build["id"]) == "ready"
    runtime = await get_runtime(s.tenant_id, "godot-node")
    assert runtime is not None
    assert runtime["image"] == f"{s.host}/pyr/t{s.tenant_id.hex}/godot-node@{DIGEST}"


async def test_a_build_is_submitted_once_even_when_advanced_twice(setup: Setup) -> None:
    s = setup
    await _define(s)
    build = await _request(s)
    await s.advance(build["id"])
    await s.advance(build["id"])
    await s.advance(build["id"])
    assert len(s.fake.submitted) == 1


async def test_an_interrupted_submission_fails_instead_of_resubmitting(setup: Setup) -> None:
    s = setup
    await _define(s)
    build = await _request(s)
    async with tenant_scope(s.tenant_id) as session:
        await session.execute(
            update(ImageBuildRow)
            .where(ImageBuildRow.id == uuid.UUID(build["id"]))
            .values(status="submitted", created_at=datetime.now(UTC) - timedelta(minutes=30))
        )
    assert await s.advance(build["id"]) == "failed"
    assert s.fake.submitted == []


async def test_the_registry_is_asked_and_a_different_digest_fails(setup: Setup) -> None:
    s = setup
    await _define(s)
    build = await _request(s)
    await s.advance(build["id"])
    s.fake.state = "succeeded"
    await s.advance(build["id"])
    assert await _verify(s, build["id"], registry_digest="sha256:" + "0" * 64) == "failed"
    assert await get_runtime(s.tenant_id, "godot-node") is None


async def test_the_same_recipe_reuses_its_digest_and_rebuild_does_not(setup: Setup) -> None:
    s = setup
    await _define(s)
    first = await _request(s)
    await s.advance(first["id"])
    s.fake.state = "succeeded"
    await s.advance(first["id"])
    await _verify(s, first["id"])
    again = await _request(s)
    assert again["reused"] and again["id"] == first["id"]
    assert len(s.fake.submitted) == 1
    fresh = await _request(s, rebuild=True)
    assert not fresh["reused"] and fresh["id"] != first["id"]


async def test_cancel_reaches_the_builder(setup: Setup) -> None:
    s = setup
    await _define(s)
    build = await _request(s)
    await s.advance(build["id"])
    await cancel_build(s.tenant_id, "godot-node", actor=s.owner)
    assert await s.advance(build["id"]) == "cancelled"
    assert s.fake.cancelled == ["run-1"]


async def test_the_operators_choices_bind(setup: Setup) -> None:
    s = setup
    await _define(s, "one")
    await _define(s, "two")
    await _request(s, "one")
    with pytest.raises(ImageError, match="as many builds"):
        await _request(s, "two")

    from core.images.builders import update_builder

    await update_builder(s.builder_key, {"allowed_tenants": [str(uuid.uuid4())]})
    with pytest.raises(ImageError, match="not available"):
        await _request(s, "two")


async def test_a_refused_dockerfile_never_reaches_a_builder(setup: Setup) -> None:
    s = setup
    await _define(s, dockerfile="FROM debian:bookworm\nCOPY . /src\n")
    with pytest.raises(ImageError, match="build context"):
        await _request(s)
    assert s.fake.submitted == []


async def test_the_registry_credential_is_lent_only_for_the_callers_own_images(
    setup: Setup,
) -> None:
    """The operator's read credential can usually read the whole registry. A path outside
    every managed root passes the namespace check when no allowlist is set -- so lending
    the credential to any reference on the host would let an organization read whatever
    else the operator keeps there."""
    import base64
    import json

    from core.images.registries import set_registry_credential
    from core.images.service import registry_pull_auth

    s = setup
    registry_key = "r" + s.builder_key[1:]
    await set_registry_credential(
        registry_key, "reader", "operator-pw", encryptor=IdentityEncryptor()
    )
    enc = IdentityEncryptor()
    own = f"{s.host}/pyr/t{s.tenant_id.hex}/godot@{DIGEST}"
    header = await registry_pull_auth(s.tenant_id, own, encryptor=enc)
    assert header is not None
    assert json.loads(base64.b64decode(header))["serveraddress"] == s.host
    for elsewhere in (
        f"{s.host}/operator-private/thing@{DIGEST}",
        f"{s.host}/pyr/t{uuid.uuid4().hex}/godot@{DIGEST}",
        f"ghcr.io/x/y@{DIGEST}",
    ):
        assert await registry_pull_auth(s.tenant_id, elsewhere, encryptor=enc) is None, elsewhere


async def test_a_github_actions_builder_is_declared_as_data(setup: Setup) -> None:
    from core.images.builders import InvalidBuilderError, set_builder_credential

    registry_key = "r" + setup.builder_key[1:]
    key = f"gh{uuid.uuid4().hex[:8]}"
    with pytest.raises(InvalidBuilderError, match="owner"):
        await create_builder(
            key,
            "github_actions",
            {
                "registry_key": registry_key,
                "config": {"owner": "a b", "repo": "r", "workflow": "w"},
            },
        )
    with pytest.raises(InvalidBuilderError, match="api_base"):
        await create_builder(
            key,
            "github_actions",
            {
                "registry_key": registry_key,
                "config": {
                    "owner": "acme",
                    "repo": "builds",
                    "workflow": "w.yml",
                    "api_base": "https://user:pw@ghes.example/api/v3",
                },
            },
        )
    builder = await create_builder(
        key,
        "github_actions",
        {
            "registry_key": registry_key,
            "config": {"owner": "acme", "repo": "builds", "workflow": "pyrrhula-image-build.yml"},
        },
    )
    try:
        assert builder.config["ref"] == "main"
        assert builder.config["api_base"] == "https://api.github.com"
        with pytest.raises(InvalidBuilderError, match="token"):
            await set_builder_credential(key, {"token": "short"}, encryptor=IdentityEncryptor())
    finally:
        await delete_builder(key)


async def test_a_harness_is_appended_by_the_platform_and_only_claimed(setup: Setup) -> None:
    s = setup
    await _define(s, dockerfile="FROM node:20-bookworm\n", harness="opencode")
    build = await _request(s)
    await s.advance(build["id"])
    sent = s.fake.submitted[0].dockerfile
    assert sent.rstrip().endswith("RUN npm i -g opencode-ai@1.18.33")
    assert build["harness_claim"] == {"key": "opencode", "version": "1.18.33"}
    assert build["baked_harness"] == {}, "a claim, until the smoke test proves it"


# ── stream builders (Portainer) ─────────────────────────────────────────────────────


@dataclass
class FakeStream:
    result: str = "succeeded"
    runs: int = 0
    mode: str = "stream"

    async def run(
        self, spec: BuildSpec, *, on_log: Any, should_cancel: Any, timeout_seconds: int
    ) -> Progress:
        self.runs += 1
        await on_log("Step 1/2 : FROM debian\n")
        if await should_cancel():
            return Progress("cancelled")
        if self.result == "failed":
            return Progress("failed", error="RUN returned 100")
        return Progress("succeeded", digest=DIGEST)

    async def probe(self) -> Any:
        raise NotImplementedError


async def _portainer_builder(s: Setup, *, ack: bool) -> str:
    from core.images.builders import update_builder

    registry_key = "r" + s.builder_key[1:]
    key = f"pt{uuid.uuid4().hex[:8]}"
    await create_builder(
        key,
        "portainer",
        {
            "registry_key": registry_key,
            "config": {"base_url": "https://portainer.lan:9443", "endpoint_id": 3},
        },
    )
    await set_builder_credential(
        key,
        {"api_key": "ptr_" + "k" * 40, "push_username": "u", "push_password": "p"},
        encryptor=IdentityEncryptor(),
    )
    if ack:
        await update_builder(key, {"isolation_ack": True})
    return key


async def test_a_portainer_builder_needs_its_isolation_acknowledged(setup: Setup) -> None:
    from core.images.builders import builders_for_tenant, update_builder

    key = await _portainer_builder(setup, ack=False)
    try:
        assert key not in {b.key for b in await builders_for_tenant(setup.tenant_id)}
        await update_builder(key, {"isolation_ack": True})
        assert key in {b.key for b in await builders_for_tenant(setup.tenant_id)}
        # A different engine is a different answer; the acknowledgement does not carry over.
        await update_builder(
            key, {"config": {"base_url": "https://other.lan:9443", "endpoint_id": 4}}
        )
        assert key not in {b.key for b in await builders_for_tenant(setup.tenant_id)}
    finally:
        await delete_builder(key)


async def test_a_stream_build_runs_once_in_its_job_and_the_sweep_leaves_it_alone(
    setup: Setup,
) -> None:
    from core.images.builds import run_stream_build

    s = setup
    key = await _portainer_builder(s, ack=True)
    stream = FakeStream()

    async def builder_for(_key: str) -> Any:
        return stream

    try:
        await _define(s)
        build = await request_build(
            s.tenant_id,
            "godot-node",
            builder_key=key,
            requested_by=s.owner,
            queue=s.queue,  # type: ignore[arg-type]
        )
        assert s.queue.jobs[-1][0] == "run_image_build"
        # The sweep must not try to submit or poll a stream build.
        assert (
            await advance_build(
                s.tenant_id,
                uuid.UUID(build["id"]),
                builder_for=builder_for,
                queue=s.queue,  # type: ignore[arg-type]
            )
            == "queued"
        )
        assert stream.runs == 0
        status = await run_stream_build(
            s.tenant_id,
            uuid.UUID(build["id"]),
            builder_for=builder_for,
            queue=s.queue,  # type: ignore[arg-type]
        )
        assert status == "verifying" and stream.runs == 1
        assert s.queue.jobs[-1][0] == "verify_image_build"
        again = await run_stream_build(
            s.tenant_id,
            uuid.UUID(build["id"]),
            builder_for=builder_for,
            queue=s.queue,  # type: ignore[arg-type]
        )
        assert again == "verifying" and stream.runs == 1, "a retried job builds nothing twice"
    finally:
        await delete_builder(key)


async def test_a_stream_build_whose_worker_went_silent_fails(setup: Setup) -> None:
    s = setup
    key = await _portainer_builder(s, ack=True)
    try:
        await _define(s)
        build = await request_build(
            s.tenant_id,
            "godot-node",
            builder_key=key,
            requested_by=s.owner,
            queue=s.queue,  # type: ignore[arg-type]
        )
        async with tenant_scope(s.tenant_id) as session:
            await session.execute(
                update(ImageBuildRow)
                .where(ImageBuildRow.id == uuid.UUID(build["id"]))
                .values(status="building", heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
            )
        assert await s.advance(build["id"]) == "failed"
    finally:
        await delete_builder(key)


async def test_host_networking_is_refused_for_a_build(setup: Setup) -> None:
    from core.images.builders import InvalidBuilderError

    with pytest.raises(InvalidBuilderError, match="host"):
        await create_builder(
            f"pt{uuid.uuid4().hex[:8]}",
            "portainer",
            {
                "registry_key": "r" + setup.builder_key[1:],
                "config": {
                    "base_url": "https://portainer.lan:9443",
                    "endpoint_id": 3,
                    "network_mode": "host",
                },
            },
        )
