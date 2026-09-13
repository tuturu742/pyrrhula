"""ExecEnvProvider port: isolated execution environments for delegated coding work.

Agents edit code and run the tests they write *somewhere* -- this port is that somewhere.
An environment is provisioned per (session, repo) from a curated runtime image, holds a
working clone of the repo, and executes shell commands (build/test) reporting exit code +
output. The v1 adapter drives sibling containers through the host's container-engine
socket (``adapters/exec_env/podman_socket``); a hosted deployment swaps in a real
orchestrator behind the same three calls. ``NullExecEnvProvider`` is the disabled state --
callers treat ``ExecEnvUnavailableError`` as "work without an environment" (the delegation
transport falls back to its direct-store path), never as a fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ExecEnvUnavailableError(Exception):
    """No environment backend is configured/reachable in this deployment."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str


class ExecEnvProvider(Protocol):
    async def provision(
        self,
        name: str,
        image: str,
        *,
        binds: list[str],
        setup_cmds: list[str],
        registry_auth: str | None = None,
    ) -> str:
        """Create (or reuse -- names are deterministic, provisioning is idempotent) a
        running environment; returns its ref (the name). ``binds`` are volume mounts
        (``volume:/path``). ``setup_cmds`` run once on first creation."""
        ...

    async def exec(self, env_ref: str, cmd: str, *, cwd: str | None = None) -> ExecResult: ...

    async def run_script(
        self,
        name: str,
        image: str,
        script: str,
        *,
        registry_auth: str | None = None,
    ) -> ExecResult:
        """One self-contained shell script in an environment named ``name`` -- the
        contract every remote engine can meet (a k8s Job, a cloud task run) and the one
        the delegation transport speaks. Socket engines keep a warm container under the
        deterministic name and exec into it; one-shot engines run a fresh task labeled
        with the name for teardown."""
        ...

    async def teardown(self, env_ref: str) -> None: ...

    async def teardown_matching(self, prefix: str) -> int:
        """Tear down every environment whose name starts with ``prefix`` (e.g. a session's
        on archive). Returns how many were removed."""
        ...


class NullExecEnvProvider:
    async def provision(
        self,
        name: str,
        image: str,
        *,
        binds: list[str],
        setup_cmds: list[str],
        registry_auth: str | None = None,
    ) -> str:
        raise ExecEnvUnavailableError("no exec-environment backend is configured")

    async def exec(self, env_ref: str, cmd: str, *, cwd: str | None = None) -> ExecResult:
        raise ExecEnvUnavailableError("no exec-environment backend is configured")

    async def run_script(
        self,
        name: str,
        image: str,
        script: str,
        *,
        registry_auth: str | None = None,
    ) -> ExecResult:
        raise ExecEnvUnavailableError("no exec-environment backend is configured")

    async def teardown(self, env_ref: str) -> None:
        return None

    async def teardown_matching(self, prefix: str) -> int:
        return 0
