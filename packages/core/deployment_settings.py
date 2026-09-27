"""Operator choices that belong to the deployment rather than to a tenant.

The retrieval models are the case this exists for. Which embedding model runs is not a
tenant decision -- every tenant's vectors sit in one column of one width, so picking per
tenant would be incoherent -- but it is also not something an operator should have to
change by finding whoever restarts the process. So: a built-in default, overridable from
the admin console, read here. There is no environment variable for it: the console is
the one place to look, and a deployment that never touches it runs the defaults the
installer already pre-downloads.

Writes take effect on the next restart: the providers hold a loaded model in memory, and
swapping that underneath a running process would change what a half-finished retrieval
means partway through. ``apply_retrieval_override`` folds the stored row into the
process-wide ``current_retrieval_models()`` at boot, which is what the synchronous
provider factories read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base
from core.tenancy.scope import unscoped_session

RETRIEVAL_MODELS_KEY = "retrieval_models"

# Multilingual, permissively licensed (MIT / Apache-2.0), acceptable on CPU -- a starting
# point, not a recommendation. The installers pre-download exactly these.
DEFAULT_EMBEDDING_MODEL = "local/BAAI/bge-m3"
DEFAULT_EMBEDDING_DIMENSION = 1024
DEFAULT_RERANKER_MODEL = "local/BAAI/bge-reranker-v2-m3"


@dataclass
class RetrievalModels:
    """What this process embeds and reranks with. ``embedding_dimension`` is the
    deployment's declared truth: the provider factories assert the selected adapter
    produces vectors of this length, so a mismatch is a loud startup error rather than a
    silent zero-recall bug found at query time."""

    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION
    reranker_model: str = DEFAULT_RERANKER_MODEL
    # False means the assembler gets ``reranker=None`` and ranking stays in WRRF order --
    # the degraded mode for a deployment too small to host a cross-encoder.
    reranker_enabled: bool = True


_current = RetrievalModels()


def current_retrieval_models() -> RetrievalModels:
    """The models this process runs on -- the built-in defaults until
    ``apply_retrieval_override`` has folded a stored admin-console choice in at boot."""
    return _current


class DeploymentSettingRow(Base):
    """Deployment-level, not tenant-scoped: no ``tenant_id``, no RLS, admin-writable
    only -- the same shape as ``plugin_repository`` and ``role_permission``."""

    __tablename__ = "deployment_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


async def get_retrieval_models() -> dict[str, Any]:
    """The effective choice: the stored override where present, the built-in default
    otherwise.

    ``source`` says which, so the admin console can show an operator whether they are
    looking at the shipped default or at something someone chose later.
    """
    defaults = RetrievalModels()
    effective: dict[str, Any] = {
        "embedding_model": defaults.embedding_model,
        "embedding_dimension": defaults.embedding_dimension,
        "reranker_model": defaults.reranker_model,
        "reranker_enabled": defaults.reranker_enabled,
        "source": "built-in default",
    }
    try:
        async with unscoped_session() as session:
            row = await session.scalar(
                text("SELECT value FROM deployment_setting WHERE key = :k").bindparams(
                    k=RETRIEVAL_MODELS_KEY
                )
            )
    except ProgrammingError:  # before the migration runs there is no table yet
        return effective
    if isinstance(row, dict) and row:
        effective.update({k: v for k, v in row.items() if k in effective})
        effective["source"] = "admin console"
    return effective


async def set_retrieval_models(values: dict[str, Any]) -> dict[str, Any]:
    """Store an override. Validates shape only -- whether the named model exists is
    answered by loading it, which happens at startup and is where a wrong name should
    fail loudly rather than here, minutes into a download."""
    embedding_model = str(values.get("embedding_model") or "").strip()
    reranker_model = str(values.get("reranker_model") or "").strip()
    if not embedding_model:
        raise ValueError("embedding_model is required")
    dimension = int(values.get("embedding_dimension") or 0)
    if dimension <= 0:
        raise ValueError("embedding_dimension must be a positive integer")

    payload = {
        "embedding_model": embedding_model,
        "embedding_dimension": dimension,
        "reranker_model": reranker_model,
        "reranker_enabled": bool(values.get("reranker_enabled", True)),
    }
    async with unscoped_session() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_setting (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ).bindparams(k=RETRIEVAL_MODELS_KEY, v=__import__("json").dumps(payload)),
        )
    return payload


SIGNUP_KEY = "signup"


async def signup_allowed() -> bool:
    """Whether strangers may create an organization (``POST /auth/signup``).

    A runtime policy the platform admin flips in the console, so it is stored here; the
    environment (``PYRRHULA_ALLOW_TENANT_SIGNUP``) only says what a fresh deployment
    starts with, which exists so an operator installing a closed instance has it closed
    before the first boot rather than after the first click."""
    from core.config import get_settings

    default = bool(get_settings().allow_tenant_signup)
    try:
        async with unscoped_session() as session:
            row = await session.scalar(
                text("SELECT value FROM deployment_setting WHERE key = :k").bindparams(k=SIGNUP_KEY)
            )
    except ProgrammingError:  # before the migration runs there is no table yet
        return default
    if isinstance(row, dict) and "allowed" in row:
        return bool(row["allowed"])
    return default


async def set_signup_allowed(allowed: bool) -> bool:
    async with unscoped_session() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_setting (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ).bindparams(k=SIGNUP_KEY, v=__import__("json").dumps({"allowed": bool(allowed)})),
        )
    return bool(allowed)


async def embedded_chunk_count() -> int:
    """How many chunks already carry a vector.

    The admin console shows this next to the embedding model because it is the cost of
    changing it: embeddings from a different model are not comparable, so every one of
    these is orphaned by a swap and the content has to be re-indexed."""
    async with unscoped_session() as session:
        return int(
            await session.scalar(
                text("SELECT count(*) FROM knowledge_chunk WHERE embedding IS NOT NULL")
            )
            or 0
        )


async def apply_retrieval_override() -> dict[str, Any] | None:
    """Fold a stored override into ``current_retrieval_models()``, once, at startup.

    The provider factories are synchronous. Rather than make every call site async to
    consult a table, the override is applied here while the process boots -- which is
    also exactly the semantics the admin console promises ("takes effect on the next
    restart"), rather than a weaker version of it.

    A database that has not run the migration yields the built-in defaults; a database
    that is not reachable raises, so the caller (``core.startup``) retries rather than
    silently running the wrong models until the next restart.
    """
    effective = await get_retrieval_models()
    if effective.get("source") != "admin console":
        return None

    for field in (
        "embedding_model",
        "embedding_dimension",
        "reranker_model",
        "reranker_enabled",
    ):
        if field in effective:
            setattr(_current, field, effective[field])
    return effective
