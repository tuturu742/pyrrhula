"""Session options: engine behaviours a session may switch, whatever its flow.

A flow sets defaults (``phase_markers``, ``recap``) in its definition; a session may
override them at creation through ``options``. These are not the flow's ``settings``:
settings are names each flow chooses for itself (a seat count, a discussion length);
options mean the same thing in every flow, so any flow can be run with them on or off --
an A/B of a behaviour is two sessions with different options, not two flow versions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.process.dsl.schema import ProcessDefinitionDSL, RecapSpec


class SessionOptions(BaseModel):
    """What a session may override. ``None`` keeps the flow's own choice."""

    model_config = ConfigDict(extra="forbid")

    phase_markers: Literal["none", "transcript", "current"] | None = None
    # "on" with a flow that declares no recap uses every section.
    recap: Literal["on", "off"] | None = None


@dataclass(frozen=True)
class EffectiveOptions:
    phase_markers: str
    recap: RecapSpec | None


def resolve_options(
    definition: ProcessDefinitionDSL | None, overrides: Mapping[str, object] | None
) -> EffectiveOptions:
    chosen = SessionOptions.model_validate(dict(overrides or {}))
    markers = chosen.phase_markers or (
        definition.phase_markers if definition is not None else "none"
    )
    flow_recap = definition.recap if definition is not None else None
    if chosen.recap == "off":
        recap = None
    elif chosen.recap == "on":
        recap = flow_recap or RecapSpec()
    else:
        recap = flow_recap
    return EffectiveOptions(phase_markers=markers, recap=recap)
