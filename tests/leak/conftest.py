"""Fixtures for the leak suite. Mirrors ``tests/isolation/conftest.py``'s and
``tests/replay/conftest.py``'s identical skip-if-unreachable pattern -- this suite needs a
live Postgres with the relevant migrations applied.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.tenancy.scope import dispose_engine, unscoped_session


@pytest_asyncio.fixture
async def db_available() -> AsyncIterator[None]:
    try:
        async with unscoped_session() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"no reachable Postgres for leak tests: {exc}")
    yield
    await dispose_engine()
