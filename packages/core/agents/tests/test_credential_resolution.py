"""#4: a stored connection key round-trips through store -> resolve, so a cloud provider
call can actually receive it. The v1 IdentityEncryptor is a no-op, but the path is real."""

from __future__ import annotations

import uuid

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import resolve_connection_api_key, store_provider_credential
from core.tenancy.seed import seed_dev_tenant


async def test_store_then_resolve_roundtrips_the_key(db_available: None) -> None:
    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"cred-{uuid.uuid4().hex[:8]}")
    enc = IdentityEncryptor()

    ref = await store_provider_credential(tenant_id, "sk-ant-secret-123", encryptor=enc)
    got = await resolve_connection_api_key(tenant_id, str(ref), encryptor=enc)
    assert got == "sk-ant-secret-123"

    # no credential_ref -> no key (local providers)
    assert await resolve_connection_api_key(tenant_id, None, encryptor=enc) is None
