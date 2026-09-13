"""godot-mcp: live Godot engine evaluation over MCP (M-D).

Two honest, self-contained tools -- no repo plumbing, no invented results:

- ``run_gdscript(code)``: run a GDScript snippet under the headless engine and return
  its real stdout/exit code. The snippet's top-level is a ``SceneTree`` script; define
  ``func run()`` and it is called once, or just override ``_init``. This is the
  "engine as a calculator" loop game-dev agents need mid-turn (verify damage math,
  vector algebra, parser behavior) -- builds/exports belong to the delegation CI path.
- ``godot_version()``: prove which engine answered.

Runs the engine with a wall-clock timeout and no display; the container image carries
the engine (see Dockerfile).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from mcp_http import McpServer

GODOT_BIN = os.environ.get("GODOT_BIN", "godot")
TIMEOUT_S = float(os.environ.get("GODOT_TIMEOUT_S", "60"))

server = McpServer("godot-mcp")

_WRAPPER = """\
extends SceneTree

{body}

func _initialize():
    if has_method("run"):
        call("run")
    quit()
"""


async def _run(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        GODOT_BIN,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        return 124, f"timed out after {TIMEOUT_S}s"
    return proc.returncode or 0, out.decode(errors="replace")


@server.tool(
    "run_gdscript",
    "Execute a GDScript snippet in the headless Godot engine and return its real "
    "output. Define `func run()` for your entry point; print() output is returned.",
    {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "GDScript source"}},
        "required": ["code"],
    },
)
async def run_gdscript(code: str = "") -> dict[str, Any]:
    body = textwrap.dedent(code)
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "snippet.gd"
        script.write_text(_WRAPPER.format(body=body))
        exit_code, output = await _run(["--headless", "--script", str(script)], cwd=tmp)
    return {"_text": output.strip() or "(no output)", "exit_code": exit_code}


@server.tool(
    "godot_version",
    "The exact Godot engine version answering these calls.",
    {"type": "object", "properties": {}},
)
async def godot_version() -> dict[str, Any]:
    exit_code, output = await _run(["--version"])
    return {"_text": output.strip(), "exit_code": exit_code}


app = server.build_app()
