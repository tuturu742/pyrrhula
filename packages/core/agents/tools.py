"""Internal tool registry: the seam the future Resolution Service plugs
real tools into. Deliberately empty by default -- no tools are hardcoded here. A caller
(a test, or the composition root) registers whatever handlers it has; the runtime
(``core.agents.runtime``) only ever sees the registry's own ``specs()``/``dispatch()``
interface, never a hardcoded
tool list.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from core.ports.model_provider import ToolCall, ToolSpec


@dataclass(frozen=True)
class ToolContext:
    tenant_id: uuid.UUID
    persona_id: uuid.UUID
    session_id: uuid.UUID | None
    # The event_seq slot the surrounding turn has already claimed for its message
    # (peeked before generation -- see run_agent_turn). Handlers that reserve their
    # own session-event seqs mid-turn must allocate PAST this slot, or the turn's
    # eventual message insert collides with them (uq_session_event_seq).
    turn_event_seq: int | None = None


@dataclass(frozen=True)
class ToolResult:
    content: str  # fed back to the model as the tool call's output message
    # A ResolutionRecord this call produced, when the handler knows it structurally.
    # A handler whose `content` is not the bare resolution JSON (the resolution preset
    # wraps it in an envelope) must set this, or the record never reaches
    # message.resolution_record_ids and the result widget has nothing to render.
    resolution_id: str | None = None


ToolHandler = Callable[[dict[str, object], ToolContext], Awaitable[ToolResult]]


class UnknownToolError(Exception):
    """The model called a tool name that isn't registered."""


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    async def dispatch(self, tool_call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Run the named tool, or answer with an error the model can act on.

        A model that calls a tool that does not exist, or calls one with arguments its
        schema rejects, gets an error *result* back -- the same channel as any other tool
        output -- so it can correct itself on the next iteration. An unknown name used to
        raise, and nothing on the session path caught it: the turn died without pausing
        the session or restoring its place in the schedule."""
        handler = self._handlers.get(tool_call.name)
        spec = self._specs.get(tool_call.name)
        if handler is None or spec is None:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "unknown_tool",
                        "detail": f"no tool named {tool_call.name!r}",
                        "available": sorted(self._handlers),
                    }
                )
            )
        arguments = coerce_scalars(spec.parameters, tool_call.arguments)
        problems = argument_errors(spec.parameters, arguments)
        if problems:
            return ToolResult(
                content=json.dumps(
                    {"error": "invalid_arguments", "tool": tool_call.name, "details": problems}
                )
            )
        return await handler(arguments, ctx)


def coerce_scalars(schema: Mapping[str, object], arguments: dict[str, object]) -> dict[str, object]:
    """Bring top-level scalar arguments to the type their property declares, where the
    conversion is lossless: 3 -> "3" for a string, "3" -> 3 for an integer, "true" ->
    True for a boolean.

    Models send `3` where a schema says string and `"true"` where it says boolean, and
    handlers written before validation existed coerced such values themselves. Refusing
    them would turn a harmless spelling into a failed call; anything that does not convert
    cleanly is left alone for the validator to report."""
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping) or not isinstance(arguments, dict):
        return arguments
    out = dict(arguments)
    for key, value in arguments.items():
        prop = properties.get(key)
        declared = prop.get("type") if isinstance(prop, Mapping) else None
        if declared == "string" and isinstance(value, int | float) and not isinstance(value, bool):
            out[key] = str(value)
        elif declared == "string" and isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif (
            declared == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit()
        ):
            out[key] = int(value.strip())
        elif declared == "integer" and isinstance(value, float) and value.is_integer():
            out[key] = int(value)
        elif declared == "number" and isinstance(value, str):
            with contextlib.suppress(ValueError):
                out[key] = float(value)
        elif (
            declared == "boolean" and isinstance(value, str) and value.lower() in ("true", "false")
        ):
            out[key] = value.lower() == "true"
    return out


def argument_errors(schema: Mapping[str, object], arguments: object, limit: int = 5) -> list[str]:
    """What is wrong with a tool call's arguments, by its JSON Schema (2020-12).

    An empty list means valid -- and also means the schema itself could not be used (a
    remote tool server's malformed schema must not make its tool uncallable; the handler
    still checks what it needs)."""
    if not schema:
        return []
    try:
        Draft202012Validator.check_schema(dict(schema))
        validator = Draft202012Validator(dict(schema))
        errors = sorted(validator.iter_errors(arguments), key=lambda e: [str(p) for p in e.path])
    except (SchemaError, ValueError, TypeError, LookupError):
        # SchemaError: the schema is malformed. The others: a valid-looking schema the
        # validator still cannot apply (an unresolvable $ref, an unknown format). Either
        # way the handler is the remaining check, not a refusal to call the tool.
        return []
    out = []
    for err in errors[:limit]:
        where = "/".join(str(p) for p in err.path) or "(arguments)"
        out.append(f"{where}: {err.message}")
    return out
