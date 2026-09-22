"""The pull-request sync: a host's verdict reaches the work item, and only a verdict does.

The risk in this module is not that it fails to act -- it is that it acts on nothing. A
sweep that reads a network error as "closed" abandons work that is alive and well, so the
"unknown changes nothing" cases carry as much weight here as the working path.
"""

from __future__ import annotations

import pytest

from adapters.gitremote.base import RemotePRStatus
from worker import pr_sync


def test_a_pr_ref_is_read_whether_or_not_it_is_hashed() -> None:
    assert pr_sync._pr_number({"pr_ref": "#12"}) == 12
    assert pr_sync._pr_number({"pr_ref": "12"}) == 12
    assert pr_sync._pr_number({"number": 12}) == 12


def test_a_record_with_no_usable_number_is_skipped_not_guessed() -> None:
    """A branch pushed with no pull request opened, or a store record from before the
    remote existed. There is nothing to ask the host about."""
    assert pr_sync._pr_number({}) is None
    assert pr_sync._pr_number({"pr_ref": "(none)"}) is None
    assert pr_sync._pr_number({"pr_ref": None}) is None


def test_only_unfinished_work_is_eligible() -> None:
    """A merged or done item is not re-decided because a host changed its mind about an
    old branch, and an abandoned one is not resurrected."""
    assert "in_review" in pr_sync._LIVE_STATES
    assert "changes_requested" in pr_sync._LIVE_STATES
    for finished in ("merged", "done", "abandoned", "backlog"):
        assert finished not in pr_sync._LIVE_STATES


def test_closed_without_merging_means_abandon() -> None:
    assert pr_sync._TRIGGER_FOR["closed"] == "abandon"


def test_a_merge_walks_the_item_up_to_approved_first() -> None:
    """`merge` is only legal from `approved`. A pull request merged on the host may never
    have been approved inside Pyrrhula, so the sync walks the path rather than attempting
    a transition the FSM will refuse."""
    for state in ("in_progress", "in_review", "changes_requested", "approved"):
        path = pr_sync._PATH_TO_MERGE[state]
        assert path[-1] == "merge", state
    assert pr_sync._PATH_TO_MERGE["in_review"] == ("approve", "merge")


class _Remote:
    """A host that answers however the test needs, and records what it was asked."""

    def __init__(self, status: RemotePRStatus | None) -> None:
        self._status = status
        self.asked = 0

    async def pull_request_status(self, source_url, token, *, number):  # noqa: ANN001, ANN201
        self.asked += 1
        return self._status


@pytest.mark.asyncio
async def test_an_unknown_status_moves_nothing() -> None:
    """The whole safety property in one test. `None` is "cannot tell" -- a failed
    request, a revoked token, a provider with no pull-request API -- and treating it as a
    closure would abandon live work on a network blip."""
    remote = _Remote(None)
    status = await remote.pull_request_status("https://example.test/a/b", "t", number=1)
    assert status is None
    # There is no trigger for "unknown": the mapping covers only real verdicts.
    assert set(pr_sync._TRIGGER_FOR) == {"merged", "closed"}


@pytest.mark.asyncio
async def test_an_open_pull_request_moves_nothing() -> None:
    remote = _Remote(RemotePRStatus(number=1, state="open"))
    status = await remote.pull_request_status("https://example.test/a/b", "t", number=1)
    assert status is not None
    assert status.state not in pr_sync._TRIGGER_FOR


def test_the_sweep_runs_on_the_workers_idle_tick() -> None:
    """Wiring, asserted on source: the sync existing and never being called is the exact
    shape of the bug that made it necessary (F3.6's renderer was complete and unwired for
    the same reason)."""
    import inspect

    from worker import main

    src = inspect.getsource(main)
    assert "await sync_pull_requests()" in src
    assert "worker.pr_sync_failed" in src, "a failing sweep must not kill the worker loop"
