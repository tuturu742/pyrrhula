import uuid

from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
from core.ports.identity import IdentityProvider
from core.tenancy.seed import seed_dev_tenant


def _unique_email() -> str:
    # (provider, external_id) is globally unique in the `identity` table (plan §12.1),
    # and these tests run against a real, persistent Postgres across repeated local/CI
    # runs -- a fixed email collides with a previous run's leftover row.
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
