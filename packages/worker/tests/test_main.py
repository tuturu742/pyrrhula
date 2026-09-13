"""Pure unit tests (no DB) for the job dispatch loop's per-job logic: success completes
the job with the handler's result, an exception fails it with the error message, and an
unknown claimed kind can't reach a handler in the first place (would raise KeyError
before ever being enqueued for `claim_one`, since `_HANDLERS` is passed as the kind
filter) -- this test proves the success/fail wiring around whatever handler runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from core.ports.job_queue import Job
from worker.main import _run_one


@dataclass
class _FakeQueue:
    completed: list[tuple[uuid.UUID, dict[str, Any] | None]] = field(default_factory=list)
    failed: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    async def complete(self, job_id: uuid.UUID, result: dict[str, Any] | None = None) -> None:
        self.completed.append((job_id, result))

    async def fail(self, job_id: uuid.UUID, error: str) -> None:
        self.failed.append((job_id, error))


def _job(kind: str) -> Job:
    return Job(id=uuid.uuid4(), tenant_id=uuid.uuid4(), kind=kind, payload={}, attempts=1)


async def test_run_one_completes_job_on_handler_success(monkeypatch) -> None:  # noqa: ANN001
    async def _ok_handler(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    import worker.main as main_module

    monkeypatch.setitem(main_module._HANDLERS, "test_kind", _ok_handler)
    queue = _FakeQueue()
    job = _job("test_kind")

    await _run_one(queue, job)  # type: ignore[arg-type]

    assert queue.completed == [(job.id, {"ok": True})]
    assert queue.failed == []


async def test_run_one_fails_job_on_handler_exception(monkeypatch) -> None:  # noqa: ANN001
    async def _boom_handler(payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("boom")

    import worker.main as main_module

    monkeypatch.setitem(main_module._HANDLERS, "test_kind", _boom_handler)
    queue = _FakeQueue()
    job = _job("test_kind")

    await _run_one(queue, job)  # type: ignore[arg-type]

    assert queue.completed == []
    assert len(queue.failed) == 1
    assert queue.failed[0][0] == job.id
    assert "boom" in queue.failed[0][1]
