"""Re-minting a share link, and what an expired one must and must not do."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from core.previews.models import PreviewEnvironmentRow
from core.previews.service import is_serveable
from core.previews.tokens import mint_preview_token, read_preview_token


def _row(**over) -> PreviewEnvironmentRow:
    row = PreviewEnvironmentRow(
        tenant_id=uuid.uuid4(),
        name="pyr-prev-abcd1234",
    )
    row.status = over.get("status", "running")
    row.internal_url = over.get("internal_url", "http://pyr-prev-abcd1234:8080")
    row.expires_at = over.get("expires_at", datetime.now(UTC) + timedelta(hours=1))
    return row


def test_a_running_unexpired_preview_is_serveable() -> None:
    assert is_serveable(_row()) is True


def test_past_the_deadline_is_not_serveable_even_while_running() -> None:
    """The reaper is periodic, so a row can still say `running` after its deadline. The
    proxy must go by the clock, not by the status alone -- otherwise a preview stays
    publicly readable in the gap between expiry and the next sweep."""
    row = _row(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert is_serveable(row) is False


def test_stopped_preview_is_not_serveable() -> None:
    assert is_serveable(_row(status="stopped")) is False
    assert is_serveable(_row(status="expired")) is False
    assert is_serveable(_row(status="failed")) is False


def test_a_preview_with_no_address_is_not_serveable() -> None:
    """A row flips to `running` only after the adapter reports an address, but a
    half-written row must never be proxied to an empty host."""
    assert is_serveable(_row(internal_url="")) is False


def test_a_freshly_minted_token_replaces_an_expired_one() -> None:
    """The share URL is derived, never stored: an expired link is re-issued for the same
    preview rather than requiring a redeploy that would drop the running container."""
    tenant_id, preview_id = uuid.uuid4(), uuid.uuid4()
    stale = mint_preview_token(tenant_id, preview_id, ttl_seconds=-1)
    assert read_preview_token(stale) is None

    fresh = mint_preview_token(tenant_id, preview_id, ttl_seconds=3600)
    assert read_preview_token(fresh) == (tenant_id, preview_id)
    assert fresh != stale


def test_reissued_tokens_address_the_same_preview() -> None:
    tenant_id, preview_id = uuid.uuid4(), uuid.uuid4()
    first = mint_preview_token(tenant_id, preview_id, ttl_seconds=3600)
    second = mint_preview_token(tenant_id, preview_id, ttl_seconds=7200)
    assert read_preview_token(first) == read_preview_token(second) == (tenant_id, preview_id)
