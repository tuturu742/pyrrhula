"""``RequestContext`` (plan §4.1): the resolved identity of a request, carried from the
edge layer through every handler. Every non-health, non-auth route requires one —
``packages/api/middleware/auth.py`` is what produces it; nothing downstream should
resolve a principal or tenant any other way.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    principal_id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None = None
