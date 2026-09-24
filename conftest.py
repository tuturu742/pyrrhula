"""Repo-root pytest configuration.

The suite rebinds its database engine every time the event loop changes -- pytest-asyncio
gives each test its own, and TestClient brings another -- so a run creates engines
constantly. Each one pools up to `pool_size + max_overflow` sockets, and an abandoned
pool's sockets are only released on a best-effort synchronous dispose, so with
SQLAlchemy's defaults (5 + 10) a full run climbed to 98 of Postgres's 100 slots. What
that looks like is not a leak: it is `TooManyConnectionsError` raised in the setup of
whichever test happened to run next, a different one each time, indistinguishable from
flakiness.

`pool_size = 0` selects `NullPool`: every session opens its own connection and closes it
on release, so an abandoned engine holds nothing. Merely shrinking the pool does not
work, because the leak is per engine and the run creates hundreds of them; a run with
`pool_size = 1` still hit the ceiling. Nor does `pool_size = 1, max_overflow = 0`, which
deadlocks -- a request needing a second session while holding the first waits for a slot
only it could release, and the run stops dead rather than failing.

It costs runtime: the suite goes from about two minutes to about five and a half,
because every session now pays for its own connect. That is the trade taken knowingly --
the two-minute run was failing two or three tests per pass, never the same ones, and a
suite that cannot be trusted to mean what it says is not faster in any sense that counts.

Production keeps SQLAlchemy's defaults, which is what `Settings` now states explicitly.
"""

import os

os.environ.setdefault("PYRRHULA_DB_POOL_SIZE", "0")
os.environ.setdefault("PYRRHULA_DB_MAX_OVERFLOW", "0")
