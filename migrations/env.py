"""Alembic environment. Async engine, URL from PYRRHULA_DATABASE_URL.

``target_metadata`` is the shared declarative base (``core.tenancy.models.Base``) with
every model module imported so autogenerate sees each table; it falls back to ``None``
when the packages are not importable, in which case ``alembic upgrade head`` still runs
the version chain and only ``alembic revision --autogenerate`` has nothing to diff.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

db_url = os.environ.get("PYRRHULA_DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = None
try:
    # Adapter/module-owned tables register themselves on this same metadata purely by
    # being imported — add new model modules here as they appear so autogenerate sees them.
    import adapters.queue.postgres.models  # noqa: E402, F401
    import core.actions.models  # noqa: E402, F401
    import core.agents.models  # noqa: E402, F401
    import core.assembler.models  # noqa: E402, F401
    import core.audit.models  # noqa: E402, F401
    import core.behavior.capabilities  # noqa: E402, F401
    import core.behavior.models  # noqa: E402, F401
    import core.knowledge.models  # noqa: E402, F401
    import core.process.models  # noqa: E402, F401
    import core.resolution.records  # noqa: E402, F401
    import core.resolution.registry  # noqa: E402, F401
    import core.resolution.rule_system  # noqa: E402, F401
    import core.secrets.models  # noqa: E402, F401
    import core.sessions.models  # noqa: E402, F401
    import core.vocabulary.models  # noqa: E402, F401
    from core.tenancy.models import Base  # noqa: E402

    target_metadata = Base.metadata
except ImportError:
    pass


# Tables created via raw SQL (op.execute) rather than the ORM — e.g. vector_store_item,
# which uses the pgvector `vector` type that isn't wired into the SQLAlchemy type system
# yet — aren't in target_metadata and would otherwise show up as "detected removed table"
# on every future autogenerate. Name them here instead of maintaining an unused ORM model
# just to keep autogenerate quiet.
_RAW_SQL_TABLES = {"vector_store_item", "knowledge_chunk"}

# Column-level sibling of _RAW_SQL_TABLES: a table that's otherwise ORM-mapped, but has
# one or more columns (a pgvector `vector` column, same reason as above) added via raw SQL
# in its migration. `secret` needs this rather than the whole-table exclusion because,
# unlike knowledge_chunk, every other column on it needs real ORM-backed CRUD from day
# one (E2.1) — see core.secrets.models.
_RAW_SQL_COLUMNS: dict[str, set[str]] = {"secret": {"gist_embedding"}}


def _include_object(object, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table" and name in _RAW_SQL_TABLES:
        return False
    if type_ == "column" and getattr(object, "table", None) is not None:
        table_name = object.table.name
        if table_name in _RAW_SQL_TABLES:
            return False
        if name in _RAW_SQL_COLUMNS.get(table_name, set()):
            return False
    return not (
        type_ == "index"
        and getattr(object, "table", None) is not None
        and object.table.name in _RAW_SQL_TABLES
    )


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=_include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
