"""Tracked exec environments: who spawned what, where, and is it still alive.

Delegated coding work runs in isolated environments (warm containers on socket
engines, one-shot Jobs/tasks on kubernetes/aws-ecs). Until now they existed only as
names inside the engine (`pyr-env-<session8>-<repo>`); this registry gives each one a
tenant-scoped row -- the spawning actor (assigned dev persona), session, engine,
image, and a live status -- so the UI can show active environments and a human can
kill a stuck one. The engine remains the source of truth for *execution*; these rows
are the source of truth for *attribution and lifecycle visibility*, written
best-effort around every run (a tracking failure never fails a delegation).

Statuses: ``running`` (a work script is executing right now), ``idle`` (a socket
engine's warm container, kept for reuse until session archive), ``completed`` /
``failed`` (a one-shot engine's run finished; exit code recorded), ``kill_requested``
(a human asked; the worker acts), ``killed``, ``removed`` (session-archive teardown).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base
from core.tenancy.scope import tenant_scope

ACTIVE_STATUSES = ("running", "idle", "kill_requested")


class ExecEnvironmentRow(Base):
    __tablename__ = "exec_environment"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    # The engine-side name (container name / Job label / ECS startedBy); the handle
    # every teardown call uses.
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    engine_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    image: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    # DB-level FK to session.id (ondelete CASCADE) lives in the migration; not declared
    # here so this module maps standalone without importing the process models.
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    spawned_by_persona_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    spawned_by_label: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default=""
    )
    last_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_exec_environment_name"),)


def _one_shot(engine_key: str | None) -> bool:
    """Socket engines keep a warm container between runs; every other kind is one-shot."""
    from core.exec_engines import engine_by_key

    engine = engine_by_key(engine_key)
    return engine is not None and str(engine.get("kind")) != "socket"


async def track_run_start(
    tenant_id: uuid.UUID,
    name: str,
    *,
    engine_key: str | None = None,
    image: str = "",
    session_id: uuid.UUID | None = None,
    repo_id: uuid.UUID | None = None,
    persona_id: uuid.UUID | None = None,
    label: str = "",
) -> str:
    """Upsert on (tenant, name): a reused warm container flips back to ``running`` and
    re-stamps the actor (the persona whose work is executing now). Returns ``"reused"``
    when a live warm environment took the run, ``"created"`` otherwise."""
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(ExecEnvironmentRow).where(
                ExecEnvironmentRow.tenant_id == tenant_id, ExecEnvironmentRow.name == name
            )
        )
        verb = "reused" if row is not None and row.status in ("idle", "running") else "created"
        if row is None:
            row = ExecEnvironmentRow(tenant_id=tenant_id, name=name)
            session.add(row)
        row.engine_key = engine_key
        row.image = image[:255]
        row.status = "running"
        row.session_id = session_id
        row.repo_id = repo_id
        row.spawned_by_persona_id = persona_id
        row.spawned_by_label = (label or "")[:120]
        row.last_exit_code = None
        return verb


async def track_run_end(tenant_id: uuid.UUID, name: str, *, exit_code: int | None) -> None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(ExecEnvironmentRow).where(
                ExecEnvironmentRow.tenant_id == tenant_id, ExecEnvironmentRow.name == name
            )
        )
        if row is None:
            return
        row.last_exit_code = exit_code
        if exit_code is None:
            row.status = "failed"
        elif _one_shot(row.engine_key):
            row.status = "completed" if exit_code == 0 else "failed"
        else:
            # The warm container survives the run, whatever the script's exit code.
            row.status = "idle"


async def mark_removed_by_prefix(
    tenant_id: uuid.UUID, prefix: str, *, status: str = "removed"
) -> int:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.scalars(
                select(ExecEnvironmentRow).where(
                    ExecEnvironmentRow.tenant_id == tenant_id,
                    ExecEnvironmentRow.name.like(prefix + "%"),
                    ExecEnvironmentRow.status.in_(ACTIVE_STATUSES),
                )
            )
        ).all()
        for row in rows:
            row.status = status
        return len(rows)


async def list_environments(
    tenant_id: uuid.UUID, *, include_finished: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    """Rows newest-activity-first, with the session's name resolved for the UI."""
    from core.sessions.models import SessionRow

    async with tenant_scope(tenant_id) as session:
        query = select(ExecEnvironmentRow).where(ExecEnvironmentRow.tenant_id == tenant_id)
        if not include_finished:
            query = query.where(ExecEnvironmentRow.status.in_(ACTIVE_STATUSES))
        rows = (
            await session.scalars(query.order_by(ExecEnvironmentRow.updated_at.desc()).limit(limit))
        ).all()
        session_ids = {r.session_id for r in rows if r.session_id is not None}
        names: dict[uuid.UUID, str] = {}
        if session_ids:
            for sid, sname in await session.execute(
                select(SessionRow.id, SessionRow.name).where(SessionRow.id.in_(session_ids))
            ):
                names[sid] = sname or ""
        return [
            {
                "id": row.id,
                "name": row.name,
                "engine_key": row.engine_key,
                "image": row.image,
                "status": row.status,
                "session_id": row.session_id,
                "session_name": names.get(row.session_id, "") if row.session_id else "",
                "repo_id": row.repo_id,
                "spawned_by_persona_id": row.spawned_by_persona_id,
                "spawned_by_label": row.spawned_by_label,
                "last_exit_code": row.last_exit_code,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]


async def get_environment(
    tenant_id: uuid.UUID, environment_id: uuid.UUID
) -> ExecEnvironmentRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ExecEnvironmentRow, environment_id)
        if row is not None:
            session.expunge(row)
        return row


async def set_status(tenant_id: uuid.UUID, environment_id: uuid.UUID, status: str) -> bool:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ExecEnvironmentRow, environment_id)
        if row is None:
            return False
        row.status = status
        return True
