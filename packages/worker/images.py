"""Worker side of ``core.images.service``: one job kind and one sweep.

``verify_image_build`` checks one build -- digest from the registry, smoke test on the
tenant's engine, promotion. Its payload is exactly ``{tenant_id, build_id}`` (plus the
queue's ``_attempt``): no reference, no credential, nothing a job row could leak.
The handler never raises for the build's own failure; a failed image is a result, and a
raised job would only be retried into the same answer.
"""

from __future__ import annotations

import uuid
from typing import Any

from adapters.image_registry.registry_v2 import RegistryV2Client
from api.encryptor_factory import get_encryptor
from core.images.service import run_verification, sweep_image_builds
from core.ports.job_queue import JobQueue
from worker.exec_env_factory import get_exec_env_provider


async def handle_verify_image_build(payload: dict[str, Any]) -> dict[str, Any]:
    status = await run_verification(
        uuid.UUID(str(payload["tenant_id"])),
        uuid.UUID(str(payload["build_id"])),
        registry_client=RegistryV2Client(),
        exec_provider_for=get_exec_env_provider,
        encryptor=get_encryptor(),
    )
    return {"status": status}


async def sweep(queue: JobQueue) -> tuple[int, int]:
    return await sweep_image_builds(queue)
