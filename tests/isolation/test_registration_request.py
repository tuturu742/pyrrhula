"""T0.4 coverage for ``registration_request``: registered in ``test_coverage_guard.py``'s
``_COVERED_TABLES``. Behavioural coverage for the policy itself lives in
``packages/api/tests/test_registration_policy.py``; this file proves only the
cross-tenant filter-omission property, which needs this directory's ``two_tenants``
fixture.

The table holds applications to join an organization, each carrying the applicant's
address and an argon2 hash of their password. A leak across tenants would disclose who
is trying to join whom -- so the isolation matters here for the row's content, not only
for tidiness.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.tenancy.registration import submit_request
from core.tenancy.scope import tenant_scope


async def test_registration_request_cross_tenant_filter_omission_returns_zero_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A raw query with no ``WHERE tenant_id``, scoped to tenant A, must never surface
    tenant B's applications -- proving RLS filters, not the query."""
    tenant_a, tenant_b = two_tenants

    for tenant_id in (tenant_a, tenant_b):
        await submit_request(
            tenant_id,
            f"applicant-{uuid.uuid4().hex[:8]}@example.com",
            "Applicant",
            "$argon2id$v=19$m=65536,t=3,p=4$placeholder",
        )

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM registration_request"))).all()

    assert {row[0] for row in rows} == {tenant_a}
