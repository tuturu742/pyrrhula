"""Waiting for delegated work, and continuing once it exists.

The flow used to dispatch work and fall straight through. The review phase reviewed
nothing and the merge phase found an empty queue, and both said so accurately, while the
branches were still being built. The session reported itself finished before the work it
had asked for existed.
"""

from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.process.dsl.schema import AwaitSpec
from worker import advance, session_wake


def test_a_phase_can_wait_for_delegated_work_not_only_for_a_person() -> None:
    spec = AwaitSpec(type="delegated_work", timeout="6h", on_timeout="review")
    assert spec.type == "delegated_work"
    # The original kind still works -- this adds a second reason to wait, it does not
    # replace the first.
    assert AwaitSpec(type="human_input", timeout="24h", on_timeout="wrap").type == "human_input"


def test_a_session_waiting_on_a_person_is_not_woken_by_a_finished_job() -> None:
    """A job completing says nothing about whether a human has acted. Waking a
    human_input await on job completion would skip the person entirely."""
    src = inspect.getsource(session_wake.wake_if_work_is_done)
    assert 'phase.await_field.type != "delegated_work"' in src


def test_done_means_no_outstanding_jobs_not_a_count_taken_at_dispatch() -> None:
    """Delegation fans out: a review per pull request, a rework per verdict, another
    review after each rework. Any number counted when the work was handed out is wrong
    by the time it matters."""
    src = inspect.getsource(session_wake._outstanding_jobs)
    assert "JobRow.status.in_(_OUTSTANDING)" in src
    assert session_wake._OUTSTANDING == ("pending", "claimed")


def test_the_job_being_finished_is_excluded_from_its_own_idle_check() -> None:
    """It is still 'claimed' while its handler returns. Counting it would mean a session
    never looks idle and every flow waits out its full timeout."""
    src = inspect.getsource(session_wake._outstanding_jobs)
    assert "JobRow.id != excluding" in src


def test_losing_the_race_to_a_timeout_is_not_an_error() -> None:
    src = inspect.getsource(session_wake.wake_if_work_is_done)
    assert "a timeout won the race" in src


def test_waking_a_session_also_arranges_for_it_to_run() -> None:
    """`satisfy_await` advances the phase and marks the session active; it does not run
    it. Stopping there leaves the flow parked one phase further on with nobody driving --
    correct, and still stuck."""
    src = inspect.getsource(session_wake.wake_if_work_is_done)
    assert '"advance_session"' in src


def test_the_worker_checks_after_every_job_kind() -> None:
    """The last job of a batch is a review or a rework at least as often as it is a
    build, so checking only after delegation would miss the tail."""
    src = inspect.getsource(__import__("worker.main", fromlist=["x"]))
    assert "wake_if_work_is_done(job.tenant_id" in src
    assert "advance_session" in src, "the resumed session needs a handler to drive it"


def test_a_resumed_advance_keeps_going_while_there_is_more_to_do() -> None:
    """`advance_session` stops at its runaway guard reporting 'active', which means
    "nothing is blocking, there is more to do". Returning there abandons the session
    mid-flow -- the same bug the HTTP path documents at its own loop."""
    src = inspect.getsource(advance.handle_advance_session)
    assert 'if result.status != "active" or result.steps_taken == 0:' in src
    assert "for _ in range(_MAX_CONTINUATIONS)" in src


# ── two workers, one session ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_worker_declines_a_session_already_being_advanced(
    monkeypatch,  # noqa: ANN001
) -> None:
    """`core.process.locking` was written to make exactly this safe and had no caller, so
    the worker advanced without claiming. One worker is fine; the deployment scales the
    worker Deployment and the queue is SKIP LOCKED precisely so it can. Two advances of
    the same session then collide on the turn's idempotency key, and the guard there does
    its job -- it refuses rather than double-charging the tenant's API key -- but the
    outcome is a failed job and a session that stopped.

    Declining is the whole behaviour: the worker that holds the claim is still going, and
    whatever enqueued this will enqueue again when there is more to do.
    """
    import worker.advance as advance
    from core.process.locking import SessionClaimTimeoutError

    tenant_id, session_id = uuid.uuid4(), uuid.uuid4()

    async def fake_get_session(_t, _s):  # noqa: ANN001
        return SimpleNamespace(
            process_definition_id=uuid.uuid4(), status="active", archived_at=None
        )

    async def fake_get_definition(_t, _d):  # noqa: ANN001
        return SimpleNamespace(definition={"any": "thing"}, version=1, id=uuid.uuid4())

    def fake_validate(_raw):  # noqa: ANN001
        return SimpleNamespace(phases={}), []

    @asynccontextmanager
    async def refusing_claim(_t, _s, _c):  # noqa: ANN001
        raise SessionClaimTimeoutError("claimed by worker:deadbeef (0.2s ago)")
        yield  # pragma: no cover

    async def must_not_run(*_a, **_k):  # noqa: ANN001  -- pragma: no cover
        raise AssertionError("a declined advance must not run a turn")

    monkeypatch.setattr("core.sessions.lifecycle.get_session", fake_get_session)
    monkeypatch.setattr(advance, "get_definition", fake_get_definition)
    monkeypatch.setattr(advance, "validate_raw", fake_validate)
    monkeypatch.setattr(advance, "claim_session", refusing_claim)
    monkeypatch.setattr(advance, "run_process_definition_session", must_not_run)

    out = await advance.handle_advance_session(
        {"tenant_id": str(tenant_id), "session_id": str(session_id)}
    )
    assert out["advanced"] is False
    assert out["reason"] == "claimed elsewhere"


@pytest.mark.asyncio
async def test_an_archived_session_is_not_advanced(monkeypatch) -> None:  # noqa: ANN001
    """Archiving drops a session out of the list, so a session that keeps advancing after
    it is invisible while it spends the tenant's API budget and holds the entity bindings
    its personas need for the next session. Two archived campaigns did exactly that, and
    the only symptom was the next campaign's players finding themselves already bound to
    characters the dice had not given them."""
    import worker.advance as advance

    async def fake_get_session(_t, _s):  # noqa: ANN001
        return SimpleNamespace(
            process_definition_id=uuid.uuid4(),
            status="active",
            archived_at="2026-09-23T07:30:00Z",
        )

    async def must_not_run(*_a, **_k):  # noqa: ANN001  -- pragma: no cover
        raise AssertionError("an archived session must not be advanced")

    monkeypatch.setattr("core.sessions.lifecycle.get_session", fake_get_session)
    monkeypatch.setattr(advance, "claim_session", must_not_run)

    out = await advance.handle_advance_session(
        {"tenant_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4())}
    )
    assert out == {"session_id": out["session_id"], "advanced": False, "reason": "archived"}
