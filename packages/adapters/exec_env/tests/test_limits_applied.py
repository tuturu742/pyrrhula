"""The limits reach the engine, in the shape each one expects.

Computing a bound and then not sending it is the failure this guards: both adapters
previously created work containers with no resource constraints at all, and nothing about
that was visible from the outside.
"""

from __future__ import annotations

from typing import Any

from adapters.exec_env.docker_socket import DockerSocketExecEnvProvider
from adapters.exec_env.kubernetes import KubernetesExecEnvProvider
from core.exec_limits import limits_for


def _host_config(engine: dict[str, Any] | None) -> dict[str, Any]:
    provider = DockerSocketExecEnvProvider("/nonexistent.sock", limits=limits_for(engine))
    return provider._host_limits()  # noqa: SLF001 -- the seam under test


def test_a_socket_container_is_bounded_by_default() -> None:
    config = _host_config(None)
    assert config["Memory"] == 4096 * 1024 * 1024
    assert config["NanoCpus"] == 2_000_000_000
    assert config["PidsLimit"] == 512


def test_swap_is_pinned_to_memory() -> None:
    """Without this the kernel may swap rather than refuse, which turns a memory limit
    into a machine that thrashes instead of one that stops."""
    config = _host_config(None)
    assert config["MemorySwap"] == config["Memory"]


def test_unlimited_omits_the_field_rather_than_sending_zero() -> None:
    """Docker reads 0 as 'no limit' for some fields and rejects it for others; omitting is
    the only spelling that means the same thing everywhere."""
    config = _host_config({"memory_mb": 0, "cpus": 0, "pids": 0})
    assert config == {}


def test_a_kubernetes_job_carries_requests_and_limits() -> None:
    provider = KubernetesExecEnvProvider({"memory_mb": 8192, "cpus": 4})
    resources = provider._resources()  # noqa: SLF001
    assert resources["limits"] == {"memory": "8192Mi", "cpu": "4.0"}
    # Requests sit well under: a build is bursty, and requesting its peak would make it
    # unschedulable on a busy cluster while reserving capacity nobody uses.
    assert resources["requests"] == {"memory": "2048Mi", "cpu": "1000m"}


def test_a_kubernetes_job_can_be_left_unbounded_on_purpose() -> None:
    provider = KubernetesExecEnvProvider({"memory_mb": 0, "cpus": 0})
    assert provider._resources() == {}  # noqa: SLF001
