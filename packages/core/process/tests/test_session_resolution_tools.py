"""The resolution preset's synthetic phase must be a VALID PhaseSpec: it is built on
every live turn in any workspace whose workflow registers a resolution server, so a
schema drift here (e.g. an ``entity_fields`` literal the DSL doesn't accept) crashes
all generation for game-like tenants before the model is ever called."""

from core.process.session_resolution_tools import _resolution_phase


def test_resolution_phase_is_a_valid_phase_spec() -> None:
    phase = _resolution_phase(["dice_roller", "coin_flip"])
    assert phase.tools == ["dice_roller", "coin_flip"]
    assert phase.visibility.entity_fields == []
    assert phase.visibility.secrets == "none"


def test_resolution_phase_accepts_empty_tool_list() -> None:
    assert _resolution_phase([]).tools == []
