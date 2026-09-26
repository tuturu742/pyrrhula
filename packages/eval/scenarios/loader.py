"""Scenario loading: the one entrypoint the harness and its tests use, so adding a
new scenario is "append to `ALL_SCENARIOS`", never touching this module.
"""

from __future__ import annotations

from eval.scenarios.fixtures import ALL_SCENARIOS
from eval.scenarios.schema import Scenario, validate_scenario


def load_all_scenarios() -> list[Scenario]:
    scenarios = list(ALL_SCENARIOS)
    for scenario in scenarios:
        validate_scenario(scenario)
    return scenarios
