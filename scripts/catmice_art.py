"""Generate the cat and mouse sprites and commit them (run inside the api pod).

argv: session_id.

Goes through the real machinery on both halves, not a shortcut:

* ``generate_image`` is called via ``core.mcp.client.call_tool``, so the workspace
  allowlist, the effectful-tool claim and the injection envelope all apply.
* the returned URL is handed to the git server's ``commit_asset``, which fetches it
  **server-side** and commits real bytes. The origin allowlist is built here from the
  workspace's registered MCP servers -- a model never supplies it.

The game loads ``res://assets/*.png`` at runtime with a drawn fallback, so this changes
how the game looks without being able to break the build.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from urllib.parse import urlparse

from sqlalchemy import select, text

# Imported for its side effect: action_record carries an FK to `session`, and SQLAlchemy
# cannot resolve it unless the session models are mapped first.
import core.sessions.models  # noqa: F401  (must precede the mcp client import)
from adapters.mcp.git_store import GitStore, default_git_root
from adapters.mcp.git_transport import GitMcpTransport
from core.mcp.client import call_tool
from core.mcp.registry import list_servers
from core.ports.mcp import McpServerRef
from core.process.dsl.schema import PhaseSpec
from core.repos.service import store_key
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope, unscoped_session

SLUG = "gamedev"
REPO_KEY = "vgame"

SPRITES = [
    (
        "assets/cat.png",
        "a cute chubby orange tabby cat facing the viewer, simple flat vector game sprite, "
        "bold clean outlines, centered, plain solid white background, no shadow, "
        "pixel-art friendly, high contrast",
    ),
    (
        "assets/mouse.png",
        "a small grey cartoon mouse with big round ears facing the viewer, simple flat "
        "vector game sprite, bold clean outlines, centered, plain solid white background, "
        "no shadow, pixel-art friendly, high contrast",
    ),
]

# The synthetic phase used for authorisation. Both tools must be listed or call_tool
# refuses them -- which is the point: this script cannot reach anything the workspace
# has not granted.
PHASE = PhaseSpec(
    label_key="phase.art",
    actors=[],
    # Nothing is disclosed to this phase: it exists only to authorise two tool names.
    visibility={
        "knowledge_classes": [],
        "scopes": [],
        "entity_fields": [],
        "secrets": "none",
    },
    tools=["generate_image", "commit_asset"],
)


async def main() -> None:
    session_id = uuid.UUID(sys.argv[1])
    run_nonce = uuid.uuid4().hex[:8]
    async with unscoped_session() as session:
        tid = await session.scalar(text("SELECT id FROM tenant WHERE slug=:s"), {"s": SLUG})
    async with tenant_scope(tid) as session:
        wid = await session.scalar(select(Workspace.id).where(Workspace.tenant_id == tid))
        seq = await session.scalar(
            text(
                "SELECT coalesce(max(event_seq), -1) + 1 FROM session_event WHERE session_id = :s"
            ),
            {"s": session_id},
        )

    servers = await list_servers(tid, wid)
    origins = sorted(
        {
            f"{urlparse(s.url).scheme}://{urlparse(s.url).netloc}"
            for s in servers
            if s.url.startswith(("http://", "https://"))
        }
    )
    print(f"registered asset origins: {origins}")

    from api.mcp_transport_factory import get_mcp_transport

    transport = get_mcp_transport()
    git = GitMcpTransport(GitStore(default_git_root()))
    git_ref = McpServerRef(key="git-vgame", url=store_key(tid, REPO_KEY))

    for i, (repo_path, prompt) in enumerate(SPRITES):
        invocation = await call_tool(
            tid,
            wid,
            session_id,
            seq + i,
            PHASE,
            "assets",
            "generate_image",
            {
                "prompt": prompt,
                "width": 256,
                "height": 256,
                # Diffusion output is opaque RGB; without this the sprite is a
                # white BOX once drawn onto the dark game background.
                "transparent_background": True,
            },
            transport=transport,
            confirmed=True,
            # A distinct attempt_target per run. Without one, the effectful-tool claim
            # correctly replays the previous invocation for the same (session, seq) --
            # which is the point of it, but means a rerun returns the OLD image.
            attempt_target=f"assets.generate_image.{repo_path}.{run_nonce}",
        )
        structured = invocation.raw.structured or {}
        asset_url = structured.get("asset_url")
        if not asset_url:
            print(f"generate_image gave no asset_url: {str(invocation.envelope)[:300]}")
            raise SystemExit(1)
        print(f"generated {repo_path}: {asset_url}")

        result = await git.call_tool(
            git_ref,
            "commit_asset",
            {
                "asset_url": asset_url,
                "repo_path": repo_path,
                "message": f"Add generated sprite {repo_path}",
                "allowed_origins": origins,
            },
        )
        print(f"committed {repo_path}: {result.structured}")


asyncio.run(main())
