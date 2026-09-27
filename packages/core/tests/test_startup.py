"""Boot hooks retry until the database answers, and one hook giving up does not stop
the next -- the shape of the fresh-install failure this replaced (an api that started
before Postgres resolved, ran its one-shot admin bootstrap, and left no admin account)."""

from __future__ import annotations

import pytest

from core.startup import run_boot_hooks


async def test_a_hook_that_fails_at_first_is_retried_until_it_succeeds() -> None:
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("Name or service not known")

    outcome = await run_boot_hooks([("flaky", flaky)], deadline_seconds=5.0, interval_seconds=0.01)
    assert outcome == {"flaky": True}
    assert calls["n"] == 3


async def test_a_hook_that_never_succeeds_gives_up_at_the_deadline_and_the_next_still_runs() -> (
    None
):
    ran: list[str] = []

    async def broken() -> None:
        raise OSError("connection refused")

    async def fine() -> None:
        ran.append("fine")

    outcome = await run_boot_hooks(
        [("broken", broken), ("fine", fine)], deadline_seconds=0.05, interval_seconds=0.01
    )
    assert outcome == {"broken": False, "fine": True}
    assert ran == ["fine"]


async def test_hooks_run_in_order_and_a_healthy_hook_runs_exactly_once() -> None:
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def second() -> None:
        order.append("second")

    outcome = await run_boot_hooks([("first", first), ("second", second)])
    assert outcome == {"first": True, "second": True}
    assert order == ["first", "second"]


@pytest.mark.parametrize("deadline", [0.0, -1.0])
async def test_a_zero_deadline_still_makes_one_attempt(deadline: float) -> None:
    attempts = {"n": 0}

    async def once() -> None:
        attempts["n"] += 1
        raise OSError("down")

    outcome = await run_boot_hooks([("once", once)], deadline_seconds=deadline)
    assert outcome == {"once": False}
    assert attempts["n"] == 1
