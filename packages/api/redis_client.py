"""Shared Redis client accessor. Used by rate limiting and SSE pub/sub fan-out
— one client, one loop-rebinding policy, not two copies that could drift.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from core.config import get_settings

_redis: Redis | None = None
_redis_loop: asyncio.AbstractEventLoop | None = None


def get_redis() -> Redis:
    """Rebinds to a fresh client whenever the running event loop changes. In production
    there is exactly one loop for the process's lifetime, so this never fires; it matters
    in tests that mix direct async calls (pytest-asyncio's loop) with ``TestClient``
    requests (its own, separate internal loop) — an async Redis connection created on one
    loop cannot be used from another. See docs/agent-guide.md for the general pattern.
    """
    global _redis, _redis_loop
    current_loop = asyncio.get_running_loop()
    if _redis is None or _redis_loop is not current_loop:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        _redis_loop = current_loop
    return _redis


async def close_redis() -> None:
    """Best-effort: only actually closes the client if called from the loop it was
    created on — see ``core.tenancy.scope.dispose_engine`` for the identical reasoning."""
    global _redis, _redis_loop
    if _redis is not None and _redis_loop is asyncio.get_running_loop():
        await _redis.aclose()
    _redis = None
    _redis_loop = None
