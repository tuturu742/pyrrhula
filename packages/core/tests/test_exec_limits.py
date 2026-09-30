"""What a delegated environment may consume.

A wall-clock timeout already exists and is not the same control: it stops a *long* run,
not a *greedy* one. With a coding harness the commands inside the container are an agent's
own choices, so a bound on what those choices can consume is what makes running them
reasonable.
"""

from __future__ import annotations

from core.exec_limits import DEFAULT_CPUS, DEFAULT_MEMORY_MB, DEFAULT_PIDS, limits_for


def test_an_engine_that_declares_nothing_is_still_bounded() -> None:
    """The case that matters: every deployment today declares no limits at all, and the
    fix is worthless if it only applies to operators who opt in."""
    limits = limits_for(None)
    assert limits.memory_mb == DEFAULT_MEMORY_MB
    assert limits.cpus == DEFAULT_CPUS
    assert limits.pids == DEFAULT_PIDS
    assert limits.memory_bytes == DEFAULT_MEMORY_MB * 1024 * 1024
    assert limits.nano_cpus == int(DEFAULT_CPUS * 1_000_000_000)


def test_an_engine_may_declare_its_own() -> None:
    """The right numbers are a property of the operator's hardware and nothing else."""
    limits = limits_for({"memory_mb": 16384, "cpus": 8, "pids": 2048})
    assert (limits.memory_mb, limits.cpus, limits.pids) == (16384, 8.0, 2048)


def test_a_declared_zero_means_unlimited_and_is_honoured() -> None:
    """An operator who has read this and decided their host needs no bounding should not
    have to fight the default -- but it has to be typed on purpose, which absence is not."""
    limits = limits_for({"memory_mb": 0, "cpus": 0, "pids": 0})
    assert (limits.memory_mb, limits.cpus, limits.pids) == (0, 0.0, 0)


def test_nonsense_falls_back_rather_than_disabling_the_bound() -> None:
    """A typo in a declaration must not silently unbound the container -- that would make
    the failure mode of a misconfiguration worse than having no feature."""
    limits = limits_for({"memory_mb": "lots", "cpus": -4, "pids": None})
    assert limits.memory_mb == DEFAULT_MEMORY_MB
    assert limits.cpus == DEFAULT_CPUS
    assert limits.pids == DEFAULT_PIDS
