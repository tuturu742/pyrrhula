"""Composition root for ``PreviewProvider`` -- engine-aware, mirroring
``worker.exec_env_factory``.

Previews run on the same operator-declared engines as exec environments (a tenant that
runs delegated work on the cluster gets its previews there too), so this reads the same
declarations rather than introducing a second registry. No declared engine => the Null
provider makes "this deployment cannot host previews" an explicit, graceful state.
"""

from __future__ import annotations

import os
from typing import Any

from adapters.preview.docker_socket import DockerSocketPreviewProvider
from core.exec_engines import engine_by_key
from core.ports.preview import NullPreviewProvider, PreviewProvider


def _build(engine: dict[str, Any]) -> PreviewProvider:
    kind = str(engine.get("kind"))
    if kind == "socket":
        socket = str(engine.get("socket") or os.environ.get("PYRRHULA_EXEC_SOCKET", ""))
        if socket and os.path.exists(socket):
            return DockerSocketPreviewProvider(
                socket, network=str(engine.get("network") or "") or None
            )
        return NullPreviewProvider()
    if kind == "kubernetes":
        from adapters.preview.kubernetes import KubernetesPreviewProvider

        return KubernetesPreviewProvider(engine)
    if kind == "aws-ecs":
        from adapters.preview.aws_ecs import AwsEcsPreviewProvider

        return AwsEcsPreviewProvider(engine)
    return NullPreviewProvider()


def get_preview_provider(engine_key: str | None = None) -> PreviewProvider:
    engine = engine_by_key(engine_key)
    if engine is None:
        return NullPreviewProvider()
    return _build(engine)
