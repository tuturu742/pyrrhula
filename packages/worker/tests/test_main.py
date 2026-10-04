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


async def test_a_failed_job_still_wakes_its_session(monkeypatch) -> None:  # noqa: ANN001
    """A session parked on its delegated work waits for the last job to finish -- and a
    job that failed has finished. Waking only on success left the session on "Working"
    until its await timed out, hours later, after a delegation's run had failed."""

    async def _boom_handler(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("engine gave up")

    woken: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _wake(tenant_id: uuid.UUID, session_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        woken.append((session_id, job_id))
        return True

    import worker.main as main_module

    monkeypatch.setitem(main_module._HANDLERS, "test_kind", _boom_handler)
    monkeypatch.setattr(main_module, "wake_if_work_is_done", _wake)
    session_id = uuid.uuid4()
    job = Job(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        kind="test_kind",
        payload={"session_id": str(session_id)},
        attempts=1,
    )

    await _run_one(_FakeQueue(), job)  # type: ignore[arg-type]

    assert woken == [(session_id, job.id)]


def test_worker_roles_split_long_builds_from_everything_else() -> None:
    import pytest

    from worker.main import _HANDLERS, claimable_kinds

    assert claimable_kinds(None) == sorted(_HANDLERS), "unset: one worker does everything"
    assert claimable_kinds("image-builder") == ["run_image_build"]
    general = claimable_kinds("general")
    assert "run_image_build" not in general and "delegate_work_item" in general
    # Quick builder steps stay with the general worker; only the long stream moves.
    assert "advance_image_build" in general and "verify_image_build" in general
    with pytest.raises(SystemExit):
        claimable_kinds("builder")
