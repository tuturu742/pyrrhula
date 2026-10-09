"""Phase markers: a flow's short line naming the moment a phase is ("Night 2 falls."),
rendered for a model either into the replayed conversation where the phase began, or as
the current phase only (``ProcessDefinitionDSL.phase_markers``).

``{name}`` in a marker reads the state variable ``name`` -- a plain lookup, never
``str.format``, so a marker cannot reach attributes or anything beyond the state's own
values. An unknown name is left as written.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_marker(template: str, state: Mapping[str, Any]) -> str:
    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(state[name]) if name in state else match.group(0)

    return _PLACEHOLDER.sub(fill, template)


def transcript_markers(definition: Any) -> dict[str, str] | None:
    """Phase key -> marker template when the flow puts markers in the transcript; None
    otherwise (and for a flow with no markers at all)."""
    if definition is None or getattr(definition, "phase_markers", "none") != "transcript":
        return None
    markers = {key: phase.marker for key, phase in definition.phases.items() if phase.marker}
    return markers or None
