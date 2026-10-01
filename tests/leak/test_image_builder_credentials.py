"""A builder's credential never lands anywhere a tenant, a log or a job row can read it.

The webhook builder holds two secrets: the signing secret and an optional bearer token.
A receiver -- or a tenant's RUN step -- can echo either back in a log line or an error
body. This drives a real ``WebhookImageBuilder`` against a receiver that does exactly
that, and then reads every place the build left a trace: every text column of the build
row, and every job payload enqueued for it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from adapters.image_builder.webhook import WebhookConfig, WebhookImageBuilder
from core.images.builders import create_builder, delete_builder, set_builder_credential
from core.images.builds import advance_build, request_build, save_definition
from core.images.models import ImageBuildRow
from core.images.registries import create_registry, delete_registry
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

pytestmark = pytest.mark.asyncio

SIGNING = "signing-SENTINEL-" + "9" * 16
TOKEN = "token-SENTINEL-" + "7" * 16


class _Queue:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def enqueue(self, tenant_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> uuid.UUID:
        self.payloads.append(payload)
        return uuid.uuid4()


def _echoing_receiver(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/submit":
        return httpx.Response(202, json={"external_id": "run-1"})
    auth = request.headers.get("authorization", "")
    return httpx.Response(
        200,
        json={
            "state": "failed",
            "log_tail": f"curl -H 'Authorization: {auth}' ... secret was {SIGNING}",
            "error": f"denied for {TOKEN}",
        },
    )


async def test_builder_secrets_do_not_reach_build_rows_or_jobs(db_available: None) -> None:
    from adapters.encryptor.identity import IdentityEncryptor

    suffix = uuid.uuid4().hex[:8]
    tenant_id, owner, _ = await seed_dev_tenant(slug=f"leak-build-{suffix}")
    await create_registry(f"r{suffix}", {"pull_host": f"leak-{suffix}.example"})
    await create_builder(
        f"b{suffix}",
        "webhook",
        {
            "registry_key": f"r{suffix}",
            "config": {
                "submit_url": "https://b.example/submit",
                "status_url": "https://b.example/status",
            },
        },
    )
    await set_builder_credential(
        f"b{suffix}", {"signing_secret": SIGNING, "token": TOKEN}, encryptor=IdentityEncryptor()
    )
    adapter = WebhookImageBuilder(
        WebhookConfig("https://b.example/submit", "https://b.example/status"),
        signing_secret=SIGNING,
        token=TOKEN,
        transport=httpx.MockTransport(_echoing_receiver),
        guard=False,
    )

    async def builder_for(_key: str) -> Any:
        return adapter

    queue = _Queue()
    try:
        await save_definition(
            tenant_id, "img", dockerfile="FROM debian:bookworm\n", harness_key="", actor=owner
        )
        build = await request_build(
            tenant_id,
            "img",
            builder_key=f"b{suffix}",
            requested_by=owner,
            queue=queue,  # type: ignore[arg-type]
        )
        for _ in range(3):
            await advance_build(
                tenant_id,
                uuid.UUID(build["id"]),
                builder_for=builder_for,
                queue=queue,  # type: ignore[arg-type]
            )
        async with tenant_scope(tenant_id) as session:
            row = await session.scalar(
                select(ImageBuildRow).where(ImageBuildRow.id == uuid.UUID(build["id"]))
            )
            assert row is not None and row.status == "failed"
            # The receiver did echo both; this is the scrubbing, not an absence of input.
            assert "[redacted]" in row.error and "[redacted]" in row.log_tail
            traces = json.dumps(
                {c.name: str(getattr(row, c.key)) for c in ImageBuildRow.__table__.columns}
            )
        traces += json.dumps(queue.payloads)
        assert SIGNING not in traces and TOKEN not in traces, "a builder secret leaked"
        assert all(set(p) == {"tenant_id", "build_id"} for p in queue.payloads)
    finally:
        await delete_builder(f"b{suffix}")
        await delete_registry(f"r{suffix}")
