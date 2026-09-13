"""Back-compat alias: the adapter speaks the plain Docker REST API over a unix
socket and always did -- podman's compat socket and docker's own socket are the
same wire protocol. Canonical home: ``adapters.exec_env.docker_socket``."""

from adapters.exec_env.docker_socket import (
    DockerSocketExecEnvProvider as PodmanSocketExecEnvProvider,
)

__all__ = ["PodmanSocketExecEnvProvider"]
