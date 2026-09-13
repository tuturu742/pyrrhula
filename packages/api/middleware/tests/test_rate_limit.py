"""Rate limiting tests (T0.6). Keys are randomised per test (uuid-based) rather than
relying on TestClient's fixed synthetic client IP, so repeated runs against a persistent
Redis don't accumulate stale counts across runs -- the same class of bug T0.4 found in
the identity adapter tests (fixed test data colliding with leftover state).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.middleware.rate_limit import (
    RateLimitExceededError,
    check_rate_limit,
    rate_limit_by_ip,
    rate_limit_by_principal,
    rate_limit_by_tenant,
)
from core.config import get_settings
from core.tenancy.context import RequestContext


async def test_exceeding_limit_raises_with_retry_after(redis_available: None) -> None:
    key = f"test-{uuid.uuid4()}"
    for _ in range(3):
        await check_rate_limit(key, limit=3, window_seconds=60)

    with pytest.raises(RateLimitExceededError) as exc_info:
        await check_rate_limit(key, limit=3, window_seconds=60)

    assert exc_info.value.retry_after_seconds > 0


async def test_different_keys_have_independent_budgets(redis_available: None) -> None:
    key_a = f"test-{uuid.uuid4()}"
    key_b = f"test-{uuid.uuid4()}"

    for _ in range(3):
        await check_rate_limit(key_a, limit=3, window_seconds=60)

    # key_a is now exhausted; key_b's separate budget must be untouched.
    await check_rate_limit(key_b, limit=3, window_seconds=60)


async def test_rate_limit_by_ip_dependency_raises_429_with_retry_after(
    redis_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_requests", 2)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)

    request = MagicMock()
    request.client.host = f"test-ip-{uuid.uuid4()}"

    await rate_limit_by_ip(request)
    await rate_limit_by_ip(request)

    with pytest.raises(HTTPException) as exc_info:
        await rate_limit_by_ip(request)

    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers
    assert int(exc_info.value.headers["Retry-After"]) > 0


async def test_rate_limit_by_principal_and_by_tenant_are_independent_budgets(
    redis_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same principal, same tenant, but the two limiters must track separate counters --
    exhausting one must not be observable through the other."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_requests", 2)
    monkeypatch.setattr(settings, "rate_limit_tenant_requests", 5)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)

    ctx = RequestContext(principal_id=uuid.uuid4(), tenant_id=uuid.uuid4())

    await rate_limit_by_principal(ctx)
    await rate_limit_by_principal(ctx)
    with pytest.raises(HTTPException) as exc_info:
        await rate_limit_by_principal(ctx)
    assert exc_info.value.status_code == 429

    # The tenant budget (limit=5) is untouched by the exhausted principal budget.
    await rate_limit_by_tenant(ctx)
