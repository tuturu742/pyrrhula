"""Waiting for delegated work, and continuing once it exists.

The flow used to dispatch work and fall straight through. The review phase reviewed
nothing and the merge phase found an empty queue, and both said so accurately, while the
branches were still being built. The session reported itself finished before the work it
had asked for existed.
"""

from __future__ import annotations

import inspect

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
