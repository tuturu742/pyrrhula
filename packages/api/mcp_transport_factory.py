"""API-side composition root for ``McpTransport``.

In-session generate turns run in the api process (live sessions' background tasks), so the
`web_search` tool needs a transport here too. No exec-env provider on this side -- the api
never runs delegated coding work; git is included read-only-capable for symmetry (the
delegate endpoints enqueue worker jobs rather than dispatching in-process).
"""

from __future__ import annotations

from adapters.mcp.composite_transport import KeyRoutingMcpTransport
from adapters.mcp.git_store import GitStore, default_git_root
from adapters.mcp.git_transport import GitMcpTransport
from adapters.mcp.remote_transport import RemoteMcpTransport
from adapters.mcp.resolution_transport import ResolutionMcpTransport
from adapters.mcp.search_transport import SearxngSearchTransport
from adapters.permission.role_permission import RolePermissionService
from core.ports.mcp import McpTransport


def get_mcp_transport() -> McpTransport:
    root = default_git_root()
    git = GitMcpTransport(GitStore(root)) if root else None
    return KeyRoutingMcpTransport(
        git=git,
        web_search=SearxngSearchTransport(),
        resolution=ResolutionMcpTransport(permission_service=RolePermissionService()),
        remote=RemoteMcpTransport(),
    )
