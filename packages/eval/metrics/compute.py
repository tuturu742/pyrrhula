"""The five metrics (E2.8, plan §8.6 (D12)): `unauthorized_disclosure_rate` (target 0 on
`full_pipeline`, by construction -- nonzero is a P0 assembler bug, not something to tune
away), `over_concealment_rate` (the counter-metric: agents must not go mute),
`directive_leak_rate` (can't be zero; track low; a content-authoring signal, not a bug),
`cross_run_consistency` (same inputs, N runs, variance), and `behavioral_fidelity` (the
blind pairwise judge -- see `eval.metrics.fidelity`, kept in its own module since it's
the one metric that needs a live model call, not pure arithmetic over `Trial`s).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from eval.metrics.schema import Trial


def unauthorized_disclosure_rate(trials: Sequence[Trial]) -> float:
    """A trial "discloses" only if the secret leaked *and* the ground truth for this
    axis value wasn't `reveal_full` -- a correct reveal is not a leak. On `full_pipeline`
    this must be 0 by construction (E2.6's exclusion); the eval harness's job is to
    prove that, not to explain away a nonzero value."""
    if not trials:
        return 0.0
    unauthorized = [t for t in trials if t.disclosed and t.expected_action != "reveal_full"]
    return len(unauthorized) / len(trials)


def over_concealment_rate(trials: Sequence[Trial]) -> float:
    """The metric most likely to be ignored (plan's own words): a system that never
    leaks because agents never say anything interesting has failed differently."""
    if not trials:
        return 0.0
    muted = [t for t in trials if t.concealed_and_silent]
    return len(muted) / len(trials)


def directive_leak_rate(trials: Sequence[Trial]) -> float:
    """Can't be zero -- a well-written directive necessarily gestures at the fact it
    guards. Tracked low, not driven to zero; a rising trend is an authoring signal
    (E2.2's empty-directive lint exists for exactly this reason), not this metric's job
    to fix."""
    if not trials:
        return 0.0
    leaks = [t for t in trials if t.directive_leak]
    return len(leaks) / len(trials)


def cross_run_consistency(trials_by_scenario: Mapping[str, Sequence[Trial]]) -> float:
    """1.0 = every repeated run of the same scenario/arm/provider/axis-value produced
    the same `action_taken`; lower = the agent's disposition drifts run to run for
    reasons other than the scripted pressure escalation itself."""
    if not trials_by_scenario:
        return 1.0
    scores: list[float] = []
    for group in trials_by_scenario.values():
        if len(group) < 2:
            continue
        actions = [t.action_taken for t in group]
        counts: dict[str, int] = defaultdict(int)
        for action in actions:
            counts[action] += 1
        scores.append(max(counts.values()) / len(actions))
    return sum(scores) / len(scores) if scores else 1.0
