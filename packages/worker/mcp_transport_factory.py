"""Composition root for `McpTransport` .

Returns the real server-side git transport (delegated coding work) when a repo root is
configured, else the unreachable stub. Everything that makes MCP calls *safe* (allowlist,
phase policy, idempotency, injection envelope) lives in `core.mcp` and is independent of what
this returns.
"""

from __future__ import annotations

import uuid
from typing import Any

from adapters.mcp.composite_transport import KeyRoutingMcpTransport
from adapters.mcp.fetch_transport import WebFetchTransport
from adapters.mcp.git_store import GitStore, default_git_root
from adapters.mcp.git_transport import GitMcpTransport
from adapters.mcp.search_transport import SearxngSearchTransport
from core.ports.mcp import McpTransport
from worker.encryptor_factory import get_encryptor
from worker.exec_env_factory import get_exec_env_provider
from worker.permission_service_factory import get_permission_service


async def _resolve_repo_token(tenant_id: str | None, credential_ref: str) -> str | None:
    """Decrypt a repo registration's stored access token at push time -- persisted call
    arguments only ever carry the opaque ref."""
    import uuid as _uuid

    from core.agents.authoring import resolve_connection_api_key

    if not tenant_id:
        return None
    return await resolve_connection_api_key(
        _uuid.UUID(tenant_id), str(credential_ref), encryptor=get_encryptor()
    )


async def _track_env(event: str, env_cfg: dict[str, Any], name: str, exit_code: int | None) -> None:
    """Best-effort exec-environment registry writes (core.exec_envs) around each run --
    the UI's 'active environments' view. The transport swallows any failure here."""
    import uuid as _uuid

    from core.exec_envs import track_run_end, track_run_start

    tenant_id = env_cfg.get("tenant_id")
    if not tenant_id:
        return

    def _uuid_or_none(value: object) -> _uuid.UUID | None:
        return _uuid.UUID(str(value)) if value else None

    if event == "start":
        verb = await track_run_start(
            _uuid.UUID(str(tenant_id)),
            name,
            engine_key=env_cfg.get("engine") or None,
            image=str(env_cfg.get("image") or ""),
            session_id=_uuid_or_none(env_cfg.get("session_id")),
            repo_id=_uuid_or_none(env_cfg.get("repo_id")),
            persona_id=_uuid_or_none(env_cfg.get("actor_persona_id")),
            label=str(env_cfg.get("actor_label") or ""),
        )
        session_id = _uuid_or_none(env_cfg.get("session_id"))
        if session_id is not None:
            await _post_env_system_event(
                _uuid.UUID(str(tenant_id)), session_id, env_cfg, name, verb
            )
    else:
        await track_run_end(_uuid.UUID(str(tenant_id)), name, exit_code=exit_code)


def _env_noun_and_url(engine_key: object) -> tuple[str, str, str]:
    """(noun, engine label, console url) for the transcript's environment line --
    engine-appropriate wording, and a link where the engine has a console."""
    from core.exec_engines import engine_by_key

    engine = engine_by_key(str(engine_key) if engine_key else None) or {}
    kind = str(engine.get("kind") or "")
    label = str(engine.get("label") or engine.get("key") or "")
    if kind == "kubernetes":
        return "pod", label, ""
    if kind == "aws-ecs":
        region, cluster = str(engine.get("region") or ""), str(engine.get("cluster") or "")
        url = (
            f"https://{region}.console.aws.amazon.com/ecs/v2/clusters/{cluster}/tasks"
            if region and cluster
            else ""
        )
        return "Fargate task", label, url
    if kind == "socket":
        return "container", label, ""
    return "environment", label, ""


async def _post_env_system_event(
    tenant_id: uuid.UUID, session_id: uuid.UUID, env_cfg: dict[str, Any], name: str, verb: str
) -> None:
    """The transcript's system line ('— container X created for task Y by Z —'),
    rendered like a phase-transition divider. Best-effort like all tracking."""
    from worker.transcript import post_system_event

    noun, engine_label, url = _env_noun_and_url(env_cfg.get("engine"))
    await post_system_event(
        tenant_id,
        session_id,
        "exec_environment",
        {
            "action": verb,
            "noun": noun,
            "name": name,
            "image": str(env_cfg.get("image") or ""),
            "engine": engine_label,
            "task": str(env_cfg.get("task_name") or ""),
            "actor": str(env_cfg.get("actor_label") or ""),
            "url": url,
        },
    )


def get_mcp_transport() -> McpTransport:
    root = default_git_root()
    # No deployment-level codegen model: every delegation carries a codegen_profile
    # derived from the ASSIGNED persona's model connection (fallback: the session's
    # supervisor persona) -- the model that writes the code is tenant data, not an
    # env var. See worker.delegation._codegen_profile_for.
    git = (
        GitMcpTransport(
            GitStore(root),
            env_provider=get_exec_env_provider(),
            env_provider_factory=get_exec_env_provider,
            codegen=None,
            token_resolver=_resolve_repo_token,
            env_tracker=_track_env,
        )
        if root
        else None
    )
    from adapters.mcp.remote_transport import RemoteMcpTransport
    from adapters.mcp.resolution_transport import ResolutionMcpTransport

    return KeyRoutingMcpTransport(
        git=git,
        web_search=SearxngSearchTransport(),
        web_fetch=WebFetchTransport(),
        resolution=ResolutionMcpTransport(permission_service=get_permission_service()),
        remote=RemoteMcpTransport(),
    )
