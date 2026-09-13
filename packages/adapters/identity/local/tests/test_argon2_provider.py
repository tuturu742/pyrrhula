import uuid

from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
from core.ports.identity import IdentityProvider
from core.tenancy.seed import seed_dev_tenant


def _unique_email() -> str:
    # Uniqueness is (tenant_id, provider, external_id), and these tests run against a
    # real, persistent Postgres across repeated local/CI runs -- a fresh email per test
    # keeps a previous run's leftover rows out of the way even within one tenant.
    return f"{uuid.uuid4().hex}@example.com"


async def test_register_then_verify_succeeds(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"ident-{uuid.uuid4().hex[:8]}")
    provider: IdentityProvider = LocalArgon2IdentityProvider()
    email = _unique_email()

    await provider.register_local(tenant_id, owner_id, email, "correct horse battery")

    identity = await provider.verify_local(tenant_id, email, "correct horse battery")
    assert identity is not None
    assert identity.principal_id == owner_id


async def test_wrong_password_fails(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"ident-{uuid.uuid4().hex[:8]}")
    provider: IdentityProvider = LocalArgon2IdentityProvider()
    email = _unique_email()

    await provider.register_local(tenant_id, owner_id, email, "correct horse battery")

    assert await provider.verify_local(tenant_id, email, "wrong password") is None


async def test_unknown_email_fails(db_available: None) -> None:
    tenant_id, _owner_id, _ = await seed_dev_tenant(slug=f"ident-{uuid.uuid4().hex[:8]}")
    provider: IdentityProvider = LocalArgon2IdentityProvider()

    assert await provider.verify_local(tenant_id, _unique_email(), "whatever") is None


async def test_wrong_tenant_fails_safe(db_available: None) -> None:
    """Same email registered in tenant A; verifying it against tenant B must fail (RLS
    shows no row), not error and not accidentally check the wrong tenant's identity."""
    tenant_a, owner_a, _ = await seed_dev_tenant(slug=f"ident-a-{uuid.uuid4().hex[:8]}")
    tenant_b, _owner_b, _ = await seed_dev_tenant(slug=f"ident-b-{uuid.uuid4().hex[:8]}")
    provider: IdentityProvider = LocalArgon2IdentityProvider()
    email = _unique_email()

    await provider.register_local(tenant_a, owner_a, email, "correct horse battery")

    assert await provider.verify_local(tenant_b, email, "correct horse battery") is None


async def test_same_email_can_own_accounts_in_different_tenants(db_available: None) -> None:
    """Tenants are independent, so an email identifies a person *within* a tenant.

    Uniqueness used to be global (provider, external_id), which meant provisioning a
    tenant could fail with "email already registered" because of a row in a different
    tenant the operator cannot see -- one tenant's occupancy leaking into another's.
    """
    tenant_a, owner_a, _ = await seed_dev_tenant(slug=f"ident-sh-a-{uuid.uuid4().hex[:8]}")
    tenant_b, owner_b, _ = await seed_dev_tenant(slug=f"ident-sh-b-{uuid.uuid4().hex[:8]}")
    provider: IdentityProvider = LocalArgon2IdentityProvider()
    email = _unique_email()

    await provider.register_local(tenant_a, owner_a, email, "correct horse battery")
    await provider.register_local(tenant_b, owner_b, email, "a different password entirely")

    in_a = await provider.verify_local(tenant_a, email, "correct horse battery")
    in_b = await provider.verify_local(tenant_b, email, "a different password entirely")
    assert in_a is not None and in_b is not None
    # Same address, two unrelated people: separate principals, and neither tenant's
    # password opens the other's account.
    assert in_a.principal_id != in_b.principal_id
    assert await provider.verify_local(tenant_a, email, "a different password entirely") is None


async def test_duplicate_email_within_one_tenant_is_still_refused(db_available: None) -> None:
    """Per-tenant, not absent: two logins with the same email inside one tenant would be
    ambiguous at verify time, so the constraint must still reject it."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from core.tenancy.provisioning import create_tenant_user

    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"ident-dup-{uuid.uuid4().hex[:8]}")
    provider: IdentityProvider = LocalArgon2IdentityProvider()
    email = _unique_email()

    await provider.register_local(tenant_id, owner_id, email, "correct horse battery")
    second = await create_tenant_user(tenant_id, "Impostor", "viewer")
    with pytest.raises(IntegrityError):
        await provider.register_local(tenant_id, second, email, "another password")
