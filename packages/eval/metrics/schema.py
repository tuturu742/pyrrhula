"""Trial observation shape (E2.8): what actually happened when one scenario ran through
one arm, for one provider, at one axis value. Reported by whoever actually drove the
scenario (a real nightly job wiring live providers; a test harness scripting fake ones)
-- the metrics module below only computes arithmetic over these, it never runs a
scenario itself.
"""

from __future__ import annotations

from dataclasses import dataclass

ARM_NAMES = ("prompt_only", "gate_no_exclusion", "full_pipeline")


@dataclass(frozen=True)
class Trial:
    scenario_key: str
    arm: str
    provider_label: str
    axis_value: int
    expected_action: str
    action_taken: str
    disclosed: bool
    directive_leak: bool
    concealed_and_silent: bool = False
    reply_text: str = ""
