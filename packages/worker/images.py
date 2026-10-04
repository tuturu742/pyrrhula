"""Worker side of ``core.images``: two job kinds and one sweep.

``advance_image_build`` takes one step of a build at an external builder (submit, poll,
cancel); ``verify_image_build`` checks one build -- digest from the registry, smoke test
on the tenant's engine, promotion. Payloads are exactly ``{tenant_id, build_id}`` (plus
the queue's ``_attempt``): no reference, no credential, nothing a job row could leak.
Handlers never raise for the build's own failure; a failed image is a result, and a
raised job would only be retried into the same answer.
"""

from __future__ import annotations

import uuid
from typing import Any

from adapters.image_registry.registry_v2 import RegistryV2Client
from api.encryptor_factory import get_encryptor
from api.image_builder_factory import load_image_builder
from core.images.builds import advance_build, sweep_builder_builds
from core.images.service import run_verification, sweep_image_builds
from core.ports.image_builder import ImageBuilder
from core.ports.job_queue import JobQueue
from worker.exec_env_factory import get_exec_env_provider


async def _builder_for(key: str) -> ImageBuilder:
    return await load_image_builder(key, encryptor=get_encryptor())


async def handle_verify_image_build(payload: dict[str, Any]) -> dict[str, Any]:
    status = await run_verification(
        uuid.UUID(str(payload["tenant_id"])),
        uuid.UUID(str(payload["build_id"])),
        registry_client=RegistryV2Client(),
        exec_provider_for=get_exec_env_provider,
        encryptor=get_encryptor(),
    )
    return {"status": status}


async def handle_advance_image_build(payload: dict[str, Any]) -> dict[str, Any]:
    from adapters.queue.postgres.queue import PostgresJobQueue

    status = await advance_build(
        uuid.UUID(str(payload["tenant_id"])),
        uuid.UUID(str(payload["build_id"])),
        builder_for=_builder_for,
        queue=PostgresJobQueue(),
    )
    return {"status": status}


async def handle_run_image_build(payload: dict[str, Any]) -> dict[str, Any]:
    """A whole build on a stream builder (Portainer). Long: see PYRRHULA_WORKER_ROLE."""
    from adapters.queue.postgres.queue import PostgresJobQueue
    from core.images.builds import run_stream_build

    status = await run_stream_build(
        uuid.UUID(str(payload["tenant_id"])),
        uuid.UUID(str(payload["build_id"])),
        builder_for=_builder_for,
        queue=PostgresJobQueue(),
    )
    return {"status": status}


async def sweep(queue: JobQueue) -> tuple[int, int]:
    """(started or moved, failed) across both halves."""
    moved = await sweep_builder_builds(queue, _builder_for)
    started, failed = await sweep_image_builds(queue)
    return started + moved, failed
