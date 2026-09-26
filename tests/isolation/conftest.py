"""Fixtures for the isolation negative-test suite.

The full per-table matrix, filter-omission coverage on every tenant-scoped table, the
pooler-leak test, and the library-tenant matrix are the job. This module's tests are
the first, smaller proof that ``tenant_scope()`` actually enforces isolation — extended,
not replaced, by the full matrix.

These tests need a live Postgres with the tenancy migration applied (the ``isolation`` CI
job in .github/workflows/ci.yml provisions exactly that). Running the full suite locally
without a database is a normal thing to do, so any test here that needs a real connection
skips cleanly instead of erroring when one isn't reachable.
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

from core.tenancy.scope import dispose_engine, unscoped_session
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture
async def db_available() -> AsyncIterator[None]:
    try:
        async with unscoped_session() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"no reachable Postgres for isolation tests: {exc}")
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def two_tenants(db_available: None) -> tuple[uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex[:8]
    tenant_a, _, _ = await seed_dev_tenant(slug=f"isolation-a-{suffix}")
    tenant_b, _, _ = await seed_dev_tenant(slug=f"isolation-b-{suffix}")
    return tenant_a, tenant_b


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def pytest_configure(config: pytest.Config) -> None:
    """Pack-shaped isolation tests read the pinned plugin content (deploy/plugins.json
    -> .plugins/), not the long-gone in-tree packs/ dir; fetch it on demand exactly as
    tests/packs does so a fresh checkout runs green."""
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


@pytest_asyncio.fixture(scope="session")
async def hagnaryd_bundle() -> pathlib.Path:
    """The Hägnaryd sample bundle, committed as a fixture (``tests/fixtures``).

    A copy of the bundle the samples repository ships, small enough to live here so the
    tests run on every machine rather than only where a sibling checkout happens to be.
    Bundles are exported from the product, never generated, so a newer copy is taken
    from the samples repository when the format changes. ``PYRRHULA_SAMPLE_BUNDLE``
    points these tests at another file instead.
    """
    import os

    override = os.environ.get("PYRRHULA_SAMPLE_BUNDLE", "")
    if override:
        path = pathlib.Path(override)
        if not path.is_file():
            pytest.skip(f"PYRRHULA_SAMPLE_BUNDLE points at nothing: {path}")
        return path
    return pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "hagnaryd-mystery.pyr"
