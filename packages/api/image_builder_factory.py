"""Composition root for the ``ImageBuilder`` port (rule 12): a declared builder, made live.

The credential is read here, at the moment of use, and handed only to the adapter -- it
never rides a job payload or a build row.
"""

from __future__ import annotations

from adapters.image_builder.webhook import WebhookConfig, WebhookImageBuilder
from core.images.builders import builder_secrets, get_builder
from core.ports.encryptor import Encryptor
from core.ports.image_builder import BuilderError, ImageBuilder


async def load_image_builder(key: str, *, encryptor: Encryptor) -> ImageBuilder:
    builder = await get_builder(key)
    if not builder.enabled:
        raise BuilderError(f"builder {key!r} is disabled")
    secrets = await builder_secrets(key, encryptor=encryptor)
    if builder.kind == "webhook":
        if not secrets.get("signing_secret"):
            raise BuilderError(f"builder {key!r} has no signing secret")
        config = builder.config
        return WebhookImageBuilder(
            WebhookConfig(
                submit_url=str(config["submit_url"]),
                status_url=str(config["status_url"]),
                cancel_url=str(config.get("cancel_url") or ""),
                insecure=bool(config.get("insecure")),
            ),
            signing_secret=secrets["signing_secret"],
            token=secrets.get("token", ""),
        )
    raise BuilderError(f"this deployment cannot drive a {builder.kind!r} builder")
