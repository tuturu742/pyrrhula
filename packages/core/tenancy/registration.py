"""Who may join an existing organization, and what happens when they ask.

``POST /auth/register`` used to create a member of whatever organization the caller
named in the ``X-Pyrrhula-Tenant`` header, gated by nothing but the per-IP rate limiter.
Its sibling ``/auth/signup`` -- which creates a *new* organization -- has always checked
``allow_tenant_signup``, and cannot be used to reach an existing one because a slug
collision allocates a fresh suffix rather than joining. So the dangerous half of the
pair was the ungated one.

Each tenant now states a policy and the deployment supplies the default:

``closed`` nobody self-registers; an admin creates accounts. The default, because
             the behaviour this replaces is the vulnerability, and a deployment that
             silently kept it would be no better off for the setting existing.
``request`` anyone may apply; an admin approves or rejects. The application holds the
             password hash so approving does not need the applicant back.
``open`` the old behaviour, now chosen rather than assumed.

The policy lives in ``tenant.settings`` because it is the organization's decision, not
the host's; the host only says what a tenant that has not decided gets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import String, Text, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base, Tenant, _uuid_pk
from core.tenancy.scope import tenant_scope, unscoped_session

RegistrationPolicy = Literal["open", "request", "closed"]

POLICIES: frozenset[str] = frozenset({"open", "request", "closed"})
SETTINGS_KEY = "registration_policy"
DEFAULT_POLICY: RegistrationPolicy = "closed"


class RegistrationRequestRow(Base):
    """An application to join a tenant, awaiting a decision."""

    __tablename__ = "registration_request"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sql_text("'pending'")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=sql_text("now()"))
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    decided_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


@dataclass(frozen=True)
class PendingRegistration:
    id: uuid.UUID
    email: str
    display_name: str
    created_at: datetime


def normalise_policy(
    value: object, *, fallback: RegistrationPolicy = DEFAULT_POLICY
) -> RegistrationPolicy:
    """A stored value that is not a policy is treated as the fallback, never as "open".

    Anything unrecognised -- a typo, a value from a newer version, a hand-edited row --
    must fail towards the closed end. The alternative is a misspelling quietly opening
    an organization to the internet.
    """
    text = str(value or "").strip().lower()
    return cast(RegistrationPolicy, text) if text in POLICIES else fallback


async def get_policy(tenant_id: uuid.UUID) -> str:
    """This tenant's policy, or the deployment default when it has not chosen."""
    from core.config import get_settings

    default = normalise_policy(get_settings().default_registration_policy)
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return default
        stored = dict(tenant.settings or {}).get(SETTINGS_KEY)
    if stored is None:
        return default
    return normalise_policy(stored, fallback=default)


async def set_policy(tenant_id: uuid.UUID, policy: str) -> str:
    """Store a tenant's choice. Raises ValueError on anything that is not a policy."""
    text = str(policy or "").strip().lower()
    if text not in POLICIES:
        raise ValueError(f"registration policy must be one of {sorted(POLICIES)}")
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"no tenant {tenant_id}")
        merged = dict(tenant.settings or {})
        merged[SETTINGS_KEY] = text
        tenant.settings = merged
    return text


class RegistrationClosedError(Exception):
    """The tenant does not accept self-registration."""


class DuplicateRequestError(Exception):
    """An application from this address is already waiting on a decision."""


async def submit_request(
    tenant_id: uuid.UUID, email: str, display_name: str, password_hash: str
) -> uuid.UUID:
    """Queue an application. The caller has already hashed the password."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(RegistrationRequestRow.id).where(
                RegistrationRequestRow.tenant_id == tenant_id,
                RegistrationRequestRow.status == "pending",
                RegistrationRequestRow.email.ilike(email),
            )
        )
        if existing is not None:
            raise DuplicateRequestError(email)
        row = RegistrationRequestRow(
            tenant_id=tenant_id,
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            status="pending",
        )
        session.add(row)
        await session.flush()
        return row.id


async def list_pending(tenant_id: uuid.UUID) -> list[PendingRegistration]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(RegistrationRequestRow)
                .where(
                    RegistrationRequestRow.tenant_id == tenant_id,
                    RegistrationRequestRow.status == "pending",
                )
                .order_by(RegistrationRequestRow.created_at)
            )
        ).scalars()
        return [
            PendingRegistration(
                id=r.id, email=r.email, display_name=r.display_name, created_at=r.created_at
            )
            for r in rows
        ]


async def take_pending(
    tenant_id: uuid.UUID, request_id: uuid.UUID, decided_by: uuid.UUID | None
) -> RegistrationRequestRow | None:
    """Claim one pending application for approval, marking it decided in the same
    statement. Returns None when it is already decided, so two admins clicking at once
    cannot both mint an account."""
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            update(RegistrationRequestRow)
            .where(
                RegistrationRequestRow.id == request_id,
                RegistrationRequestRow.tenant_id == tenant_id,
                RegistrationRequestRow.status == "pending",
            )
            .values(
                status="approved",
                decided_at=sql_text("now()"),
                decided_by_principal_id=decided_by,
            )
            .returning(RegistrationRequestRow)
        )
        if row is None:
            return None
        session.expunge(row)
        return row


async def reject(
    tenant_id: uuid.UUID, request_id: uuid.UUID, decided_by: uuid.UUID | None, note: str | None
) -> bool:
    """Refuse an application and drop the password hash it was holding."""
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            update(RegistrationRequestRow)
            .where(
                RegistrationRequestRow.id == request_id,
                RegistrationRequestRow.tenant_id == tenant_id,
                RegistrationRequestRow.status == "pending",
            )
            .values(
                status="rejected",
                note=(note or None),
                # A refusal has no use for the applicant's credential, and keeping it
                # would mean a rejected stranger's password hash living in the table
                # indefinitely.
                password_hash="",
                decided_at=sql_text("now()"),
                decided_by_principal_id=decided_by,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)
