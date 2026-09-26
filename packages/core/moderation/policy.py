"""Per-tenant moderation policy.

 identifies two scans as the *real* controls, and this module is what they consult:

* **at authoring** -- secrets, knowledge entries, and personas are user-authored text, and
  a DB-connected scanner reads them at write time. Crucially it reads a secret's `content`
  whatever its disclosure state: concealment is about what reaches a *model*, and a scanner
  that respected it would be a scanner the secrets system blinded.
* **at generation** -- replies are visible and are scanned before delivery.

Neither depends on an overseer existing, which is the point: Q6's rule is that a
multi-human workspace needs an overseer **or** moderation with overseer-equivalent
visibility, and an arm that only worked when an overseer was present would satisfy nothing.

**Policy lives in `tenant.settings`, and the default is permissive** ('s seam
principle). A platform that arrived pre-censoring would be a platform tenants fight; one
that cannot be configured to block anything is a platform enterprises cannot buy. So: a
seam, off by default, with the categories and the action a tenant's own decision.

The `action` vocabulary is closed -- `flag | block | queue` -- for the same reason
`usage_record.purpose` is: an invented fourth value is a behaviour nobody reviewed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from core.tenancy.models import Tenant
from core.tenancy.scope import unscoped_session

Action = Literal["flag", "block", "queue"]
_ACTIONS: frozenset[str] = frozenset({"flag", "block", "queue"})

SETTINGS_KEY = "moderation"


@dataclass(frozen=True)
class ModerationPolicy:
    """``enabled=False`` is the default and means every scan passes without a provider
    call. Not "scan and ignore the result" -- a disabled policy makes no call at all, so a
    tenant that never opted in never sends their text anywhere."""

    enabled: bool = False
    action: Action = "flag"
    categories: tuple[str, ...] = ()
    # Overseer-equivalent visibility: does this tenant's moderation surface flagged content
    # to a human who can act on it? Q6's second arm turns on this, not on `enabled` --
    # scanning that nobody reads is not oversight.
    overseer_equivalent: bool = False

    @property
    def blocks(self) -> bool:
        return self.enabled and self.action == "block"


def parse_policy(settings: dict[str, Any]) -> ModerationPolicy:
    """Reads a policy out of `tenant.settings`, tolerating absence and malformed values by
    falling back to the permissive default. A tenant whose settings are unreadable gets the
    behaviour they had before anyone touched them, which is the only safe direction for a
    parse error in a control to fail."""
    raw = settings.get(SETTINGS_KEY)
    if not isinstance(raw, dict):
        return ModerationPolicy()

    action = raw.get("action", "flag")
    if action not in _ACTIONS:
        action = "flag"
    categories = raw.get("categories", [])
    return ModerationPolicy(
        enabled=bool(raw.get("enabled", False)),
        action=action,
        categories=tuple(str(c) for c in categories) if isinstance(categories, list) else (),
        overseer_equivalent=bool(raw.get("overseer_equivalent", False)),
    )


async def get_policy(tenant_id: uuid.UUID) -> ModerationPolicy:
    """Reads through `unscoped_session` because `tenant` is one of the three tables
    CLAUDE.md names as legitimately outside RLS -- a tenant row is what *defines* a tenant,
    and cannot be filtered by one."""
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        settings = dict(tenant.settings) if tenant is not None else {}
    return parse_policy(settings)


async def set_policy(tenant_id: uuid.UUID, policy: ModerationPolicy) -> None:
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"no tenant {tenant_id}")
        tenant.settings = {
            **dict(tenant.settings),
            SETTINGS_KEY: {
                "enabled": policy.enabled,
                "action": policy.action,
                "categories": list(policy.categories),
                "overseer_equivalent": policy.overseer_equivalent,
            },
        }


@dataclass(frozen=True)
class ScanOutcome:
    """What a scan decided. ``reasons`` are the provider's category labels -- never the
    text itself, which is the same reason the audit row records a target id rather than
    content: a moderation log full of the content it flagged is a second copy of
    every sensitive thing anyone wrote."""

    allowed: bool
    action_taken: str
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return not self.allowed
