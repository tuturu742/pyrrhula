"""The worker's composition root for ``Notifier`` selection -- mirrors
``worker.model_provider_factory``. ``core`` never imports ``adapters``; this is where the
concrete channel is chosen, and it is the only file that changes when a deployment swaps
the log default for real email.
"""

from __future__ import annotations

from adapters.notifier.log_notifier import LogNotifier
from core.ports.notifier import Notifier


def get_notifier() -> Notifier:
    return LogNotifier()
