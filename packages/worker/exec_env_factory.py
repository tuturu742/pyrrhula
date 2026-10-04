"""Composition root for ``ExecEnvProvider`` — engine-aware.

The operator's declarations (``core.exec_engines.declared_engines``) name which engines
this deployment offers; the tenant's chosen key rides each delegation's environment
config, and the transport asks this factory for the matching provider per call. No
declared engines (and no legacy socket) => the Null provider makes "no environments" an
explicit, graceful state (delegation falls back to the direct-store path), never a
fault.
"""

from __future__ import annotations

import os
from typing import Any

from adapters.exec_env.docker_socket import DockerSocketExecEnvProvider
from core.exec_engines import engine_by_key
from core.exec_limits import limits_for
from core.ports.exec_env import ExecEnvProvider, NullExecEnvProvider


def _build(engine: dict[str, Any]) -> ExecEnvProvider:
    kind = str(engine.get("kind"))
    if kind == "socket":
        socket = str(engine.get("socket") or os.environ.get("PYRRHULA_EXEC_SOCKET", ""))
        if socket and os.path.exists(socket):
            return DockerSocketExecEnvProvider(
                socket,
                network=str(engine.get("network") or "") or None,
                limits=limits_for(engine),
                run_timeout_seconds=int(engine.get("run_timeout_seconds") or 1800),
            )
        return NullExecEnvProvider()
    if kind == "kubernetes":
        from adapters.exec_env.kubernetes import KubernetesExecEnvProvider

        return KubernetesExecEnvProvider(engine)
    if kind == "aws-ecs":
        from adapters.exec_env.aws_ecs import AwsEcsExecEnvProvider

        return AwsEcsExecEnvProvider(engine)
    return NullExecEnvProvider()


def get_exec_env_provider(engine_key: str | None = None) -> ExecEnvProvider:
    engine = engine_by_key(engine_key)
    if engine is None:
        return NullExecEnvProvider()
    return _build(engine)
