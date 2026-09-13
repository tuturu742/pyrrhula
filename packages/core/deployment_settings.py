"""Operator choices that belong to the deployment rather than to a tenant.

The retrieval models are the case this exists for. Which embedding model runs is not a
tenant decision -- every tenant's vectors sit in one column of one width, so picking per
tenant would be incoherent -- but it is also not something an operator should have to
change by finding whoever restarts the process. So: an env default, overridable from the
admin console, read here.

Reads fall back to configuration, so a deployment that never touches the admin console
behaves exactly as its environment says. Writes take effect on the next restart: the
providers hold a loaded model in memory, and swapping that underneath a running process
would change what a half-finished retrieval means partway through.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base
from core.tenancy.scope import unscoped_session

RETRIEVAL_MODELS_KEY = "retrieval_models"


class DeploymentSettingRow(Base):
    """Deployment-level, not tenant-scoped: no ``tenant_id``, no RLS, admin-writable
    only -- the same shape as ``plugin_repository`` and ``role_permission``."""

    __tablename__ = "deployment_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


async def get_retrieval_models() -> dict[str, Any]:
    """The effective choice: the stored override where present, configuration otherwise.

    ``source`` says which, so the admin console can show an operator whether they are
    looking at this deployment's environment or at something someone changed later.
    """
    from core.config import get_settings

    settings = get_settings()
    effective: dict[str, Any] = {
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "reranker_model": settings.reranker_model,
        "reranker_enabled": settings.reranker_enabled,
        "source": "environment",
    }
    try:
        async with unscoped_session() as session:
            row = await session.scalar(
                text("SELECT value FROM deployment_setting WHERE key = :k").bindparams(
                    k=RETRIEVAL_MODELS_KEY
                )
            )
    except Exception:  # noqa: BLE001 -- before the migration runs there is no table yet
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


async def apply_retrieval_override_to_settings() -> dict[str, Any] | None:
    """Fold a stored override into the process's cached Settings, once, at startup.

    The provider factories are synchronous and read ``get_settings()``. Rather than make
    every call site async to consult a table, the override is applied here while the
    process boots -- which is also exactly the semantics the admin console promises
    ("takes effect on the next restart"), rather than a weaker version of it.

    Best-effort: a deployment whose database is not reachable yet, or has not run the
    migration, keeps its environment configuration.
    """
    from core.config import get_settings

    try:
        effective = await get_retrieval_models()
    except Exception:  # noqa: BLE001 -- never block startup on an optional override
        return None
    if effective.get("source") != "admin console":
        return None

    settings = get_settings()
    for field in (
        "embedding_model",
        "embedding_dimension",
        "reranker_model",
        "reranker_enabled",
    ):
        if field in effective:
            object.__setattr__(settings, field, effective[field])
    return effective
