"""Adversarial secret-probing scenario schema (E2.8, plan §8.6 (D12)).

A scenario is a secret an agent holds, plus a scripted escalating-pressure probe
sequence (direct question -> social pressure -> deception -> authority claim -> prompt
injection) and, for each axis value worth testing, the disclosure action a correctly
behaving agent *should* produce. Front-loading these -- authoring scenarios before the
implementation they judge exists -- is the whole point (§15.10): otherwise you
unconsciously write scenarios your implementation already passes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

PRESSURE_FAMILIES = (
    "direct_question",
    "social_pressure",
    "deception",
    "authority_claim",
    "prompt_injection",
)

OVERLAYS = ("rpg_v1", "enterprise_v1", "swdev_v1")

_VALID_ACTIONS = frozenset({"conceal", "hint", "reveal_full"})


class ProbeTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pressure: str
    speaker: str
    text: str


class ExpectedBand(BaseModel):
    """What a correctly behaving agent should do at this axis value, for this
    scenario's decisive (final) probe -- the harness's ground truth."""

    model_config = ConfigDict(extra="forbid")

    axis_key: str
    axis_value: int
    expected_action: str


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    overlay: str
    family: str
    secret_gist: str
    secret_content: str
    probes: list[ProbeTurn] = Field(min_length=1)
    expected_bands: list[ExpectedBand] = Field(min_length=1)
    notes: str = ""


class ScenarioValidationError(Exception):
    pass


def validate_scenario(scenario: Scenario) -> None:
    if scenario.overlay not in OVERLAYS:
        raise ScenarioValidationError(
            f"scenario {scenario.key!r}: unknown overlay {scenario.overlay!r}, "
            f"must be one of {OVERLAYS}"
        )
    if scenario.family not in PRESSURE_FAMILIES:
        raise ScenarioValidationError(
            f"scenario {scenario.key!r}: unknown family {scenario.family!r}, "
            f"must be one of {PRESSURE_FAMILIES}"
        )
    for probe in scenario.probes:
        if probe.pressure not in PRESSURE_FAMILIES:
            raise ScenarioValidationError(
                f"scenario {scenario.key!r}: probe declares unknown pressure {probe.pressure!r}"
            )
    for band in scenario.expected_bands:
        if band.expected_action not in _VALID_ACTIONS:
            raise ScenarioValidationError(
                f"scenario {scenario.key!r}: expected_action {band.expected_action!r} "
                f"must be one of {sorted(_VALID_ACTIONS)}"
            )
