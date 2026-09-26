"""An in-memory ``Notifier`` that keeps what it was handed. Lives in ``adapters/`` rather
than in a test file because both the adapter contract test and the core tests need it,
and a double defined inside one test module is a double the other one copies."""

from __future__ import annotations

from core.ports.notifier import Notification


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)
