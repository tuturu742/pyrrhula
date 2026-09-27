"""Boot-time setup that waits for the database instead of giving up on it.

The api and the worker each run a few idempotent hooks when they start: sync the baked
workflow plugin, ensure the platform-admin account, fold the admin console's retrieval
override into the process. On a fresh install those processes routinely start a few
seconds before the database answers -- Kubernetes brings every pod up at once, and the
service name does not even resolve until Postgres is ready -- and a hook that ran once,
failed on "Name or service not known" and logged a warning left a deployment with no
admin account until somebody restarted the api. Found on a clean install.

So each hook is retried until it succeeds or the deadline passes. Retrying is safe
because every hook is idempotent by construction. The loop runs *before* the process
starts serving: an api without its database is not ready, and a readiness probe that
says otherwise is the thing that hid this.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

import structlog

log = structlog.get_logger()

Hook = Callable[[], Awaitable[object]]

DEFAULT_DEADLINE_SECONDS = 300.0
DEFAULT_INTERVAL_SECONDS = 2.0


async def run_boot_hooks(
    hooks: Sequence[tuple[str, Hook]],
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> dict[str, bool]:
    """Run each hook in order until it succeeds or the shared deadline passes.

    Returns ``{name: succeeded}``. A hook that keeps failing is logged at error level and
    the next one still runs: a missing retrieval override must not stop the admin
    account from being created, and vice versa. The first failure of each hook is logged
    as a warning with the reason; later attempts stay quiet until they succeed or give up.
    """
    started = time.monotonic()
    outcome: dict[str, bool] = {}
    for name, hook in hooks:
        attempts = 0
        while True:
            attempts += 1
            try:
                await hook()
            except Exception as exc:  # noqa: BLE001 -- the reason is logged and retried
                remaining = deadline_seconds - (time.monotonic() - started)
                if attempts == 1:
                    log.warning(f"{name}.failed", error=str(exc)[:300], retrying=remaining > 0)
                if remaining <= 0:
                    log.error(f"{name}.gave_up", attempts=attempts, error=str(exc)[:300])
                    outcome[name] = False
                    break
                await asyncio.sleep(min(interval_seconds, max(remaining, 0.0)))
                continue
            if attempts > 1:
                log.info(f"{name}.succeeded_after_retry", attempts=attempts)
            outcome[name] = True
            break
    return outcome
