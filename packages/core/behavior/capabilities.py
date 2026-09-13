"""Capability matrix storage (E2.9, plan §8.6/§13.6): `model_capability` turns E2.8's
per-provider eval results into an enforced capability surface. Global, not tenant-scoped
-- like `price_table`, this is the platform's own knowledge of what a (provider, model)
pair can actually do, not tenant data (`unscoped_session()`, no RLS).

**Data-driven, not code-driven** (this task's own acceptance criterion): mutating a
stored row flips what `is_axis_capable`/`get_capability` report without touching a line
of code -- the nightly E2.8 run's job is to keep these rows current, not to encode
capability logic here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, Text, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base
from core.tenancy.scope import unscoped_session


class ModelCapabilityRow(Base):
    __tablename__ = "model_capability"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    axis_key: Mapped[str] = mapped_column(String(63), nullable=False)
    capable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_output_fidelity: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    behavioral_fidelity: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "model", "axis_key", name="uq_model_capability"),
    )


async def upsert_capability(
    provider: str,
    model: str,
    axis_key: str,
    capable: bool,
    *,
    reason: str | None = None,
    structured_output_fidelity: float | None = None,
    behavioral_fidelity: float | None = None,
) -> ModelCapabilityRow:
    """The write side of "data-driven": this is what a nightly E2.8 run (or a test
    mutating the stored result) calls -- never a code change to flip a capability."""
    async with unscoped_session() as session:
        existing = await session.scalar(
            select(ModelCapabilityRow).where(
                ModelCapabilityRow.provider == provider,
                ModelCapabilityRow.model == model,
                ModelCapabilityRow.axis_key == axis_key,
            )
        )
        if existing is not None:
            existing.capable = capable
            existing.reason = reason
            existing.structured_output_fidelity = (
                Decimal(str(structured_output_fidelity))
                if structured_output_fidelity is not None
                else None
            )
            existing.behavioral_fidelity = (
                Decimal(str(behavioral_fidelity)) if behavioral_fidelity is not None else None
            )
            await session.flush()
            return existing

        row = ModelCapabilityRow(
            provider=provider,
            model=model,
            axis_key=axis_key,
            capable=capable,
            reason=reason,
            structured_output_fidelity=(
                Decimal(str(structured_output_fidelity))
                if structured_output_fidelity is not None
                else None
            ),
            behavioral_fidelity=(
                Decimal(str(behavioral_fidelity)) if behavioral_fidelity is not None else None
            ),
        )
        session.add(row)
        await session.flush()
        return row


async def get_capability(provider: str, model: str, axis_key: str) -> ModelCapabilityRow | None:
    async with unscoped_session() as session:
        row = await session.scalar(
            select(ModelCapabilityRow).where(
                ModelCapabilityRow.provider == provider,
                ModelCapabilityRow.model == model,
                ModelCapabilityRow.axis_key == axis_key,
            )
        )
        return row


async def list_capabilities_for_model(provider: str, model: str) -> list[ModelCapabilityRow]:
    async with unscoped_session() as session:
        rows = (
            await session.execute(
                select(ModelCapabilityRow).where(
                    ModelCapabilityRow.provider == provider, ModelCapabilityRow.model == model
                )
            )
        ).scalars()
        return list(rows)


async def is_axis_capable(provider: str, model: str, axis_key: str) -> bool:
    """No stored row at all (never evaluated) is permissive, not fail-closed -- matching
    D14's own "missing policy => permissive" convention: forcing every never-evaluated
    model to be blocked would make the system unusable before any eval ever ran. A
    *known* failure (a row that says `capable=False`) is what actually blocks."""
    capability = await get_capability(provider, model, axis_key)
    return capability is None or capability.capable
