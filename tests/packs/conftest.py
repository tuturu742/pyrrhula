"""Fixtures for the pack-matrix harness. Mirrors ``tests/isolation/conftest.py``'s
``db_available``/``two_tenants`` shape -- a separate, self-contained conftest rather than
a cross-package import, matching this repo's existing convention of each test suite
owning its own fixtures.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import dispose_engine, tenant_scope, unscoped_session
from core.tenancy.seed import seed_dev_tenant

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def pytest_configure(config: pytest.Config) -> None:
    """Pack content lives in the pinned plugin repos (deploy/plugins.json), not
    in-tree; fetch on demand so `pytest tests/packs` works from a fresh checkout."""
    _ensure_packs(_REPO_ROOT)


def _ensure_packs(repo_root: pathlib.Path) -> None:
    """Fetch the pinned plugin repos, and say so plainly when they cannot be reached.

    These suites assert properties OF the shipped packs, so there is no meaningful
    fallback -- `builtin-workflows` does not contain them. Without the content the tests
    used to die on a FileNotFoundError several frames deep, which reads like a broken
    test rather than an unreachable repository. Fail at collection with the actual cause.
    """
    packs = repo_root / ".plugins" / "default"
    if (packs / "plugin.json").is_file():
        return
    subprocess.run([sys.executable, str(repo_root / "scripts" / "fetch_plugins.py")], check=False)
    if not (packs / "plugin.json").is_file():
        raise pytest.UsageError(
            "workflow pack content is missing and could not be fetched.\n"
            f"  {packs} has no plugin.json.\n"
            "  These suites assert properties of the SHIPPED packs, so there is no\n"
            "  fallback -- builtin-workflows does not contain them.\n"
            "  Check that the repository pinned in deploy/plugins.json is reachable from\n"
            "  here (a private repo is not, over unauthenticated HTTPS), or point that\n"
            "  file at a copy you can read."
        )


@pytest_asyncio.fixture
async def db_available() -> AsyncIterator[None]:
    try:
        async with unscoped_session() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"no reachable Postgres for pack-matrix tests: {exc}")
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def pack_tenant(db_available: None) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A fresh tenant + workspace + a principal granted ``facilitator`` on it (enough
    permission for the mutation-service calls the smoke harness makes) -- returns
    ``(tenant_id, workspace_id, principal_id)``."""
    suffix = uuid.uuid4().hex[:8]
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"pack-matrix-{suffix}")

    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=owner_id,
                role="facilitator",
            )
        )

    return tenant_id, workspace_id, owner_id
