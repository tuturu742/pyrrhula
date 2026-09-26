"""Notifier port. "It's your turn" has to leave the
building somehow, and *how* is a deployment decision -- email today, a chat webhook or a
push channel tomorrow -- while *when* and *to whom* are product decisions that belong in
core. This port is that line.

Deliberately minimal: one method, no templating, no threading, no delivery receipts. A
notification is already-rendered text addressed to one principal; everything an adapter
needs to route it (the principal's address, the tenant's sender identity) it looks up or
is configured with itself, because core has no business knowing a principal has an email
address at all.

``dedupe_key`` is carried on the notification rather than left to the adapter: "exactly
one notification, one reminder" is a promise Pyrrhula makes, and a promise kept by an
adapter is a promise kept differently by every adapter. ``core.sessions.notifications``
enforces it with a unique constraint before an adapter is ever called; the key travels
along so an adapter with its own idempotency (most transactional-email providers have one)
can use the same one rather than inventing a second.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Notification:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    kind: str
    subject: str
    body_md: str
    dedupe_key: str


class Notifier(Protocol):
    async def send(self, notification: Notification) -> None: ...
