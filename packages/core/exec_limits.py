"""What a delegated environment may consume.

An exec environment runs code an agent wrote, and with a coding harness it runs commands
an agent *chose* -- the harness is invoked with its approvals on, because a loop that stops
at the first edit waiting for a human who is not there is not a loop. That is the intended
behaviour, not a hole. What makes it safe to intend is that the container is bounded.

Today it is not. The socket adapter sets no memory, CPU or process limits, and the
Kubernetes adapter writes no ``resources`` block, so one runaway test suite is the host's
problem and a fork bomb is everyone's. A wall-clock timeout exists and is not the same
thing: it stops a long run, not a greedy one.

Declared per engine, because the right numbers are a property of the hardware the operator
is running on and nothing else. The defaults here are deliberately generous -- a real build
is memory-hungry and a test suite is CPU-hungry, and a limit that fails honest work would
be turned off within the week, which is worse than a loose one that stays on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Enough for a Rust or JVM build, which is the hungriest thing these environments do. The
# point is to bound a runaway, not to size the job.
DEFAULT_MEMORY_MB = 4096
DEFAULT_CPUS = 2.0
# A build legitimately spawns hundreds of processes; a fork bomb spawns as many as it can.
# This sits far above the first and far below the second.
DEFAULT_PIDS = 512


@dataclass(frozen=True)
class ExecLimits:
    memory_mb: int
    cpus: float
    pids: int

    @property
    def memory_bytes(self) -> int:
        return self.memory_mb * 1024 * 1024

    @property
    def nano_cpus(self) -> int:
        """Docker's CPU quota unit: 1e9 == one core."""
        return int(self.cpus * 1_000_000_000)


def limits_for(engine: dict[str, Any] | None) -> ExecLimits:
    """The declared limits for one engine, falling back to the defaults.

    A declared zero means *unlimited*, and is honoured: an operator who has read this and
    decided their host does not need bounding should not have to fight the default. It has
    to be typed on purpose, though, which absence is not.
    """
    declared = engine or {}

    def _number(key: str, fallback: float) -> float:
        value = declared.get(key)
        if isinstance(value, int | float) and value >= 0:
            return float(value)
        return fallback

    return ExecLimits(
        memory_mb=int(_number("memory_mb", DEFAULT_MEMORY_MB)),
        cpus=_number("cpus", DEFAULT_CPUS),
        pids=int(_number("pids", DEFAULT_PIDS)),
    )
