"""Rate limiting. Implemented as a Redis fixed-window counter (``INCR`` +
``EXPIRE``) — simpler than a true token bucket and sufficient for v1; noted here rather
than overclaimed, since "token bucket" implies smoothing this doesn't do.

Three keying dimensions, all through the same ``check_rate_limit()``: auth endpoints (no
principal yet) key by client IP; protected endpoints key by ``principal_id`` (bounds one
account's abuse) and separately by ``tenant_id`` (bounds many principals in one tenant
collectively overwhelming shared resources — a different failure mode than any single
principal misbehaving).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from api.middleware.auth import get_request_context
from api.redis_client import get_redis
from core.config import get_settings
from core.tenancy.context import RequestContext


class RateLimitExceededError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"rate limit exceeded, retry after {retry_after_seconds}s")


async def check_rate_limit(
    key: str, *, limit: int | None = None, window_seconds: int | None = None
) -> None:
    settings = get_settings()
    limit = limit if limit is not None else settings.rate_limit_requests
    window_seconds = (
        window_seconds if window_seconds is not None else settings.rate_limit_window_seconds
    )

    redis = get_redis()
    redis_key = f"ratelimit:{key}"
    count = await redis.incr(redis_key)
    if count == 1:
        await redis.expire(redis_key, window_seconds)

    if count > limit:
        ttl = await redis.ttl(redis_key)
        raise RateLimitExceededError(retry_after_seconds=max(ttl, 1))


async def _raise_429(exc: RateLimitExceededError) -> None:
    raise HTTPException(
        status_code=429,
        detail="rate limit exceeded",
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


async def rate_limit_by_ip(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    try:
        await check_rate_limit(f"ip:{client_ip}")
    except RateLimitExceededError as exc:
        await _raise_429(exc)


async def rate_limit_by_principal(ctx: RequestContext = Depends(get_request_context)) -> None:
    try:
        await check_rate_limit(f"principal:{ctx.principal_id}")
    except RateLimitExceededError as exc:
        await _raise_429(exc)


async def rate_limit_by_tenant(ctx: RequestContext = Depends(get_request_context)) -> None:
    settings = get_settings()
    try:
        await check_rate_limit(
            f"tenant:{ctx.tenant_id}",
            limit=settings.rate_limit_tenant_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    except RateLimitExceededError as exc:
        await _raise_429(exc)
