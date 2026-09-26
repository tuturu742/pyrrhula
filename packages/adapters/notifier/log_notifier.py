"""v1 ``Notifier``: structured-log delivery.

Not a placeholder for a missing decision -- a deliberate default. The notification pipeline
(who is notified, exactly once, with what visibility-filtered content) is complete and
tested without any particular channel existing, and this environment has no mail server,
SMTP credentials, or webhook endpoint to send to. Wiring a real channel is a
composition-root change (``worker.notifier_factory``) with no core impact: that is the
whole point of the port.

A log line is also a genuinely useful production default for a self-hosted deployment that
has not configured mail yet -- it fails visibly rather than silently swallowing "it's your
turn".
"""

from __future__ import annotations

import structlog

from core.ports.notifier import Notification

log = structlog.get_logger()


class LogNotifier:
    async def send(self, notification: Notification) -> None:
        log.info(
            "notifier.send",
            tenant_id=str(notification.tenant_id),
            principal_id=str(notification.principal_id),
            kind=notification.kind,
            subject=notification.subject,
            dedupe_key=notification.dedupe_key,
        )
