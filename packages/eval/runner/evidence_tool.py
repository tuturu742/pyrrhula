"""The `evidence_check` tool for the Hägnaryd benchmark.

The case brief gives the investigator a menu of lab requests, at most TWO, whose answers
are referee-only ground truth. This wires that as a real in-turn tool: the investigator
agent calls `evidence_check(request=...)`, the handler returns the referee's answer, and
a shared budget counter enforces the two-request cap across the whole session (the model
cannot simply keep asking).

The oracle is eval-only. It never touches production -- it is passed through
`run_one_persona_turn(extra_tools=...)`, the fenced in-process seam, and only for the
investigator persona.
"""

from __future__ import annotations

import json

from core.agents.tools import ToolContext, ToolResult
from core.ports.model_provider import ToolSpec
from eval.scenarios.hagnaryd_case import REFEREE_LAB_RESULTS

EVIDENCE_TOOL_SPEC = ToolSpec(
    name="evidence_check",
    description=(
        "Send a forensic lab request by radio. You may make at most TWO across the "
        "whole interview; results come back immediately. Choose carefully."
    ),
    parameters={
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "enum": sorted(REFEREE_LAB_RESULTS.keys()),
                "description": "The lab request to run.",
            }
        },
        "required": ["request"],
        "additionalProperties": False,
    },
)


class EvidenceBudget:
    """Session-scoped state for the two-request cap. One instance per benchmark run;
    the handler closes over it so every investigator turn shares the same counter."""

    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self.used: list[str] = []

    def make_handler(self):  # noqa: ANN201 -- returns a ToolHandler
        async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
            del ctx
            request = str(args.get("request", "")).strip()
            if request not in REFEREE_LAB_RESULTS:
                return ToolResult(
                    content=json.dumps(
                        {
                            "error": "unknown_request",
                            "available": sorted(REFEREE_LAB_RESULTS),
                        }
                    )
                )
            if request in self.used:
                # a repeat doesn't spend budget -- just return what it already said
                return ToolResult(
                    content=json.dumps({"request": request, "result": REFEREE_LAB_RESULTS[request]})
                )
            if len(self.used) >= self.limit:
                return ToolResult(
                    content=json.dumps(
                        {
                            "error": "budget_exhausted",
                            "message": f"only {self.limit} lab requests are allowed; "
                            f"you have used {self.used}",
                        }
                    )
                )
            self.used.append(request)
            return ToolResult(
                content=json.dumps({"request": request, "result": REFEREE_LAB_RESULTS[request]})
            )

        return handler
