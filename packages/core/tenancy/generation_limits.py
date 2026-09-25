"""Per-tenant ceilings on a single generation -- ``settings["generation_limits"]``.

These are circuit breakers, not budgets: they exist because a model can stop stopping,
and nothing else in the stack notices. The job lease measures SILENCE, and a runaway is
not silent. An output cap measures CHARACTERS, and the worst case emits none, because a
reasoning model's hidden thinking never reaches content. Only a clock sees a turn that is
doing neither. Measured live before these existed: one turn of 470,244 characters over 79
minutes, which then became the next turn's prompt and did it again; others of 22 and 58
minutes that produced nothing at all.

Tenant settings rather than environment: what a workspace's models may do is policy about
how that workspace plays, and policy belongs where the person deciding is already looking
(see the rule `docs/configuration.md` is audited against). A deployment fact would be
which host answers; "how long may a turn run" is not one.

Shaped exactly like ``core.tenancy.egress``: the same TTL cache, loaded once per
``GenerationRequest`` construction and attached to it, enforced inside the ModelProvider
port. An absent or unparseable value falls back to the defaults rather than to no limit --
these fail CLOSED, because the failure they guard against is unbounded.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from core.tenancy.models import Tenant
from core.tenancy.scope import unscoped_session

_TTL_SECONDS = 30.0

# Against a longest legitimate turn of 65 seconds measured across every sample here, and
# a longest legitimate reply of a few thousand characters. Both sit far enough above an
# honest turn that they never shape output, and close enough that a stuck model is cut
# off in minutes rather than the seventy-nine it took unguarded.
DEFAULT_MAX_SECONDS = 300.0
DEFAULT_MAX_CHARS = 100_000

SETTINGS_KEY = "generation_limits"


@dataclass(frozen=True)
class GenerationLimits:
    max_seconds: float = DEFAULT_MAX_SECONDS
    max_chars: int = DEFAULT_MAX_CHARS


DEFAULT_LIMITS = GenerationLimits()

_cache: dict[uuid.UUID, tuple[float, GenerationLimits]] = {}


def _coerce(raw: object) -> GenerationLimits:
    if not isinstance(raw, dict):
        return DEFAULT_LIMITS
    try:
        seconds = float(raw.get("max_seconds", DEFAULT_MAX_SECONDS))
        chars = int(raw.get("max_chars", DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_LIMITS
    # Zero or negative would mean "no ceiling", which is the state these exist to end.
    return GenerationLimits(
        max_seconds=seconds if seconds > 0 else DEFAULT_MAX_SECONDS,
        max_chars=chars if chars > 0 else DEFAULT_MAX_CHARS,
    )


async def load_generation_limits(tenant_id: uuid.UUID) -> GenerationLimits:
    now = time.monotonic()
    hit = _cache.get(tenant_id)
    if hit is not None and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    limits = _coerce((tenant.settings or {}).get(SETTINGS_KEY) if tenant else None)
    _cache[tenant_id] = (now, limits)
    return limits


def invalidate_generation_limits(tenant_id: uuid.UUID) -> None:
    _cache.pop(tenant_id, None)
