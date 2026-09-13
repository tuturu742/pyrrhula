"""One-shot: seal identity-era plaintext credentials with the configured key.

    podman exec pyrrhula_api_1 python -m worker.reencrypt          # dry run
    podman exec pyrrhula_api_1 python -m worker.reencrypt --yes    # for real

Requires ``PYRRHULA_ENCRYPTION_KEY`` on the container. Idempotent: rows already
version-prefixed (``enc1:``) are skipped, so re-running after a partial run finishes
the job. Rotating to a NEW key later needs decrypt-with-old + encrypt-with-new -- out
of scope here (this command only wraps plaintext).
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from adapters.encryptor.aesgcm import AesGcmEncryptor
from core.agents.models import ProviderCredentialRow
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope, unscoped_session
from worker.encryptor_factory import get_encryptor


async def main(apply: bool) -> None:
    encryptor = get_encryptor()
    if not isinstance(encryptor, AesGcmEncryptor):
        raise SystemExit("PYRRHULA_ENCRYPTION_KEY is not set -- nothing to seal with.")

    async with unscoped_session() as session:
        tenant_ids = [t.id for t in (await session.execute(select(Tenant))).scalars()]

    sealed = skipped = 0
    for tenant_id in tenant_ids:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(ProviderCredentialRow).where(
                        ProviderCredentialRow.tenant_id == tenant_id
                    )
                )
            ).scalars()
            for row in rows:
                if AesGcmEncryptor.is_sealed(row.ciphertext):
                    skipped += 1
                    continue
                if apply:
                    row.ciphertext = encryptor.encrypt(row.ciphertext)
                sealed += 1

    verb = "sealed" if apply else "WOULD seal (dry run; pass --yes)"
    print(f"{verb}: {sealed} credential(s); already sealed: {skipped}")


if __name__ == "__main__":
    asyncio.run(main(apply="--yes" in sys.argv))
