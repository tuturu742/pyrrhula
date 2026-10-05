"""Read tools for the workspace assistant that answer under the asking user's own
permissions.

The first nine read tools call tenant-level services that every member may see. The ones
here reach things a member may *not* be entitled to -- secrets, entities, a workspace's
members and settings, the tenant's configuration -- so each one asks the
``PermissionService`` about the viewer before it reads, exactly as the matching GET route
does (CLAUDE.md rule 12), and refuses with a short JSON error the model can relay. Secrets
are projected to gists only: plaintext, hints and directives never reach the model
(INV-8), whoever is asking.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from core.agents.models import Persona
from core.agents.tools import ToolContext, ToolRegistry, ToolResult
from core.assembler.visibility import EXPORT, scopes_for
from core.entities.injection import visible_fields
from core.entities.repo import get_schema, list_latest_schemas
from core.entities.storage import get_entity, list_all_entities_for_workspace
from core.entities.validation import compute_derived
from core.exec_engines import declared_engines, get_tenant_engine_key
from core.mcp.registry import list_servers
from core.ports.encryptor import Encryptor
from core.ports.model_provider import ToolSpec
from core.ports.permission import PermissionService, UnknownActionError
from core.previews.service import list_previews
from core.reporting.pipeline import list_reports_for_session
from core.reporting.templates import BUILT_IN_TEMPLATES
from core.repos.runtimes import resolved_runtimes
from core.secrets.authoring import get_secret_view, list_holders, list_secret_views_for_workspace
from core.sessions.lifecycle import get_session, list_session_roster
from core.tenancy.models import Identity, Principal, Workspace, WorkspaceMembership
from core.tenancy.preferences import get_preferences
from core.tenancy.scope import tenant_scope
from core.usage_limits import get_limits
from core.vocabulary.service import list_overlays, resolve_overlay_for_workspace

_ENTITY_LIST_CAP = 100


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def _s(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def refused(action: str) -> ToolResult:
    return ToolResult(content=json.dumps({"error": f"not permitted: {action}"}))


class ViewerGate:
    """``allowed(action)`` for the asking user on this workspace (or the tenant)."""

    def __init__(
        self,
        permission_service: PermissionService,
        tenant_id: uuid.UUID,
        viewer_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> None:
        self._svc = permission_service
        self._tenant = tenant_id
        self._viewer = viewer_id
        self._workspace = workspace_id

    async def allowed(self, action: str, *, tenant_level: bool = False) -> bool:
        try:
            if tenant_level:
                return await self._svc.check(
                    self._tenant, self._viewer, action, "tenant", self._tenant
                )
            return await self._svc.check(
                self._tenant, self._viewer, action, "workspace", self._workspace
            )
        except UnknownActionError:
            return False


def register_viewer_read_tools(
    registry: ToolRegistry,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    *,
    encryptor: Encryptor,
    permission_service: PermissionService,
) -> None:
    gate = ViewerGate(permission_service, tenant_id, viewer.id, workspace_id)

    def _dump(value: object) -> ToolResult:
        return ToolResult(content=json.dumps(value, default=str))

    # ── secrets: gists only ────────────────────────────────────────────────────────
    async def _list_secrets(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        views = await list_secret_views_for_workspace(
            tenant_id,
            workspace_id,
            viewer.id,
            encryptor=encryptor,
            permission_service=permission_service,
        )
        # The view carries plaintext for an author; the projection below is what
        # reaches the model, and it never includes content, hint_text or the directive.
        return _dump(
            [
                {
                    "id": str(v.id),
                    "subject_kind": v.subject_kind,
                    "subject_id": str(v.subject_id),
                    "gist": v.gist,
                    "scope_key": v.scope_key,
                    "publication": v.publication,
                    "disclosure_state": v.disclosure_state,
                    "version": v.version,
                    "is_author": v.is_author,
                }
                for v in views
            ]
        )

    async def _list_secret_holders(args: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        secret_id = uuid.UUID(str(args["secret_id"]))
        view = await get_secret_view(
            tenant_id,
            secret_id,
            viewer.id,
            encryptor=encryptor,
            permission_service=permission_service,
        )
        if view is None or view.workspace_id != workspace_id:
            return _dump({"error": "no such secret in this workspace"})
        rows = await list_holders(tenant_id, secret_id)
        names = await _principal_names(tenant_id, {r.holder_principal_id for r in rows})
        return _dump(
            [
                {
                    "holder_id": str(r.id),
                    "holder_principal_id": str(r.holder_principal_id),
                    "holder_kind": r.holder_kind,
                    "name": names.get(r.holder_principal_id),
                }
                for r in rows
            ]
        )

    # ── entities and schemas: the viewer's own field visibility ───────────────────
    async def _list_entities(args: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        wanted = str(args.get("schema_key") or "") or None
        rows = await list_all_entities_for_workspace(tenant_id, workspace_id)
        keys: dict[uuid.UUID, str] = {}
        out = []
        for row in rows:
            if row.schema_id not in keys:
                schema_row = await get_schema(tenant_id, row.schema_id)
                keys[row.schema_id] = schema_row.key if schema_row is not None else ""
            if wanted and keys[row.schema_id] != wanted:
                continue
            out.append(
                {
                    "id": str(row.id),
                    "key": row.key,
                    "name": row.name,
                    "schema_key": keys[row.schema_id],
                    "fsm_states": dict(row.fsm_states),
                    "origin_session_id": str(row.origin_session_id)
                    if row.origin_session_id
                    else None,
                }
            )
        out.sort(key=lambda item: str(item["name"]))
        return _dump(out[:_ENTITY_LIST_CAP])

    async def _get_entity(args: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        entity = await get_entity(tenant_id, uuid.UUID(str(args["entity_id"])))
        if entity is None or entity.workspace_id != workspace_id:
            return _dump({"error": "no such entity in this workspace"})
        schema_row = await get_schema(tenant_id, entity.schema_id)
        if schema_row is None:
            return _dump({"error": "the entity's schema no longer exists"})
        definition = schema_row.to_definition()
        scope_set = await scopes_for(tenant_id, viewer.id, workspace_id, EXPORT, None)
        visible = visible_fields(entity, definition, frozenset(scope_set))
        transitions = []
        for machine in definition.state_machines:
            current = entity.fsm_states.get(machine.key, machine.initial)
            for t in machine.transitions:
                if t.from_state in (current, "*"):
                    transitions.append(
                        {"machine_key": machine.key, "trigger": t.trigger, "to": t.to}
                    )
        return _dump(
            {
                "id": str(entity.id),
                "key": entity.key,
                "name": entity.name,
                "schema_key": schema_row.key,
                "version": entity.version,
                "fields": dict(sorted(visible.items())),
                "derived": compute_derived(definition, entity.data),
                "fsm_states": dict(entity.fsm_states),
                "transitions": transitions,
            }
        )

    def _schema_summary(row: Any, *, full: bool) -> dict[str, object]:
        definition = row.to_definition()
        summary: dict[str, object] = {
            "key": row.key,
            "version": row.version,
            "workspace_id": str(row.workspace_id) if row.workspace_id else None,
            "field_keys": [f.key for f in definition.fields],
            "machines": {
                m.key: sorted({t.trigger for t in m.transitions}) for m in definition.state_machines
            },
        }
        if full:
            summary["definition"] = definition.model_dump(by_alias=True, mode="json")
        return summary

    async def _list_entity_schemas(args: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        rows = list(await list_latest_schemas(tenant_id, workspace_id))
        if str(args.get("include_templates") or "").lower() in ("true", "1", "yes"):
            rows += list(await list_latest_schemas(tenant_id, None))
        return _dump([_schema_summary(r, full=False) for r in rows])

    async def _get_entity_schema(args: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        key = str(args["key"])
        template = str(args.get("template") or "").lower() in ("true", "1", "yes")
        rows = await list_latest_schemas(tenant_id, None if template else workspace_id)
        row = next((r for r in rows if r.key == key), None)
        if row is None:
            return _dump({"error": f"no schema {key!r}"})
        return _dump(_schema_summary(row, full=True))

    # ── the workspace itself ──────────────────────────────────────────────────────
    async def _get_workspace(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        can_manage = await gate.allowed("manage_workspace")
        async with tenant_scope(tenant_id) as session:
            ws = await session.get(Workspace, workspace_id)
            if ws is None:
                return _dump({"error": "no such workspace"})
            settings = dict(ws.settings or {})
            clock = getattr(ws, "clock_value", None)
            name, key = ws.name, ws.key
            member_rows = (
                await session.execute(
                    select(
                        WorkspaceMembership.principal_id,
                        WorkspaceMembership.role,
                        Principal.display_name,
                        Principal.kind,
                        Identity.external_id,
                    )
                    .join(Principal, Principal.id == WorkspaceMembership.principal_id)
                    .join(
                        Identity,
                        (Identity.principal_id == Principal.id) & (Identity.provider == "local"),
                        isouter=True,
                    )
                    .where(WorkspaceMembership.workspace_id == workspace_id)
                )
            ).all()
        overlay = await resolve_overlay_for_workspace(tenant_id, workspace_id)
        wanted = (
            "secret_mode",
            "conduct_rules",
            "allow_automerge",
            "max_review_rounds",
            "moderation_model",
            "assistant_context_max_tokens",
        )
        return _dump(
            {
                "id": str(workspace_id),
                "name": name,
                "key": key,
                "clock_value": clock,
                "vocabulary_overlay": overlay.key if overlay is not None else None,
                "settings": {k: settings.get(k) for k in wanted if k in settings},
                "members": [
                    {
                        "principal_id": str(pid),
                        "display_name": display,
                        "role": role,
                        "is_persona": kind != "human",
                        **({"email": email} if can_manage and email else {}),
                    }
                    for pid, role, display, kind, email in member_rows
                ],
            }
        )

    async def _list_workspaces(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        select(Workspace).where(
                            Workspace.tenant_id == tenant_id, Workspace.archived_at.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            return _dump([{"id": str(w.id), "key": w.key, "name": w.name} for w in rows])

    # ── sessions and reports ──────────────────────────────────────────────────────
    async def _get_session(args: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        session_id = uuid.UUID(str(args["session_id"]))
        row = await get_session(tenant_id, session_id)
        if row is None or row.workspace_id != workspace_id:
            return _dump({"error": "no such session in this workspace"})
        roster = await list_session_roster(tenant_id, session_id)
        names = await _persona_names(tenant_id, {r.persona_id for r in roster})
        reports = await list_reports_for_session(tenant_id, session_id)
        return _dump(
            {
                "id": str(row.id),
                "name": row.name,
                "status": row.status,
                "phase": row.current_phase,
                "turn_policy": row.turn_policy,
                "agenda_md": row.agenda_md,
                "roster": [
                    {
                        "persona_id": str(r.persona_id),
                        "name": names.get(r.persona_id),
                        "is_supervisor": r.is_supervisor,
                    }
                    for r in roster
                ],
                "reports": [
                    {"id": str(r.id), "template_key": r.template_key, "audience": r.audience_mode}
                    for r in reports
                ],
            }
        )

    async def _list_report_templates(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        return _dump(
            [{"key": key, "title": t.title or t.label_key} for key, t in BUILT_IN_TEMPLATES.items()]
        )

    # ── the tenant's configuration, in one read ───────────────────────────────────
    async def _get_tenant_config(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        prefs = await get_preferences(tenant_id)
        runtimes = await resolved_runtimes(tenant_id)
        servers = await list_servers(tenant_id, workspace_id)
        previews = await list_previews(tenant_id, workspace_id=workspace_id)
        return _dump(
            {
                "limits": await get_limits(tenant_id),
                "preferences": {
                    "session_lifetime_seconds": prefs.session_lifetime_seconds,
                    "preview_ttl_seconds": prefs.preview_ttl_seconds,
                    "reranker_enabled": prefs.reranker_enabled,
                },
                "exec_engines": [e.get("key") for e in declared_engines()],
                "current_exec_engine": await get_tenant_engine_key(tenant_id),
                "runtimes": [
                    {"key": k, "image": v.get("image"), "builtin": not v.get("built")}
                    for k, v in runtimes.items()
                ],
                "vocabulary_overlays": [o.key for o in await list_overlays(tenant_id)],
                "mcp_servers": [
                    {
                        "key": s.key,
                        "url": s.url,
                        "enabled_tools": list(s.enabled_tools or []),
                        "effectful_tools": list(s.effectful_tools or []),
                        "require_confirmation": s.require_confirmation,
                        "max_calls_per_session": s.max_calls_per_session,
                        "enabled": s.enabled,
                    }
                    for s in servers
                ],
                "previews": [
                    {k: p.get(k) for k in ("id", "name", "status", "repo_id", "expires_at")}
                    for p in previews
                ],
            }
        )

    for spec, handler in (
        (
            ToolSpec(
                name="list_secrets",
                description=(
                    "The workspace's secrets as gists (never their content): id, subject, "
                    "scope, publication, disclosure state, and whether you authored it."
                ),
                parameters=_obj({}, []),
            ),
            _list_secrets,
        ),
        (
            ToolSpec(
                name="list_secret_holders",
                description="Who holds one secret: holder ids, principal ids and names.",
                parameters=_obj({"secret_id": _s("secret id (from list_secrets)")}, ["secret_id"]),
            ),
            _list_secret_holders,
        ),
        (
            ToolSpec(
                name="list_entities",
                description="Entities in this workspace (names and states), optionally one schema.",
                parameters=_obj({"schema_key": _s("schema key to filter by (optional)")}, []),
            ),
            _list_entities,
        ),
        (
            ToolSpec(
                name="get_entity",
                description=(
                    "One entity: the fields you may see, derived values, its states, and the "
                    "transitions available from them (machine_key + trigger)."
                ),
                parameters=_obj({"entity_id": _s("entity id (from list_entities)")}, ["entity_id"]),
            ),
            _get_entity,
        ),
        (
            ToolSpec(
                name="list_entity_schemas",
                description="Entity schemas (keys, versions, field keys, state machines).",
                parameters=_obj(
                    {"include_templates": _s("'true' to include tenant templates")}, []
                ),
            ),
            _list_entity_schemas,
        ),
        (
            ToolSpec(
                name="get_entity_schema",
                description="One entity schema's full definition, to edit and resubmit.",
                parameters=_obj(
                    {"key": _s("schema key"), "template": _s("'true' for a tenant template")},
                    ["key"],
                ),
            ),
            _get_entity_schema,
        ),
        (
            ToolSpec(
                name="get_workspace",
                description=(
                    "This workspace: settings, clock, vocabulary overlay, and members with "
                    "their principal ids and roles."
                ),
                parameters=_obj({}, []),
            ),
            _get_workspace,
        ),
        (
            ToolSpec(
                name="list_workspaces",
                description="The organization's workspaces (id, key, name).",
                parameters=_obj({}, []),
            ),
            _list_workspaces,
        ),
        (
            ToolSpec(
                name="get_session",
                description="One session: status, phase, turn policy, agenda, roster, reports.",
                parameters=_obj(
                    {"session_id": _s("session id (from list_sessions)")}, ["session_id"]
                ),
            ),
            _get_session,
        ),
        (
            ToolSpec(
                name="list_report_templates",
                description="The report templates a session can be rendered with.",
                parameters=_obj({}, []),
            ),
            _list_report_templates,
        ),
        (
            ToolSpec(
                name="get_tenant_config",
                description=(
                    "The organization's configuration: usage limits, preferences, exec "
                    "engines, build runtimes, vocabulary overlays, and this workspace's MCP "
                    "servers and previews."
                ),
                parameters=_obj({}, []),
            ),
            _get_tenant_config,
        ),
    ):
        registry.register(spec, handler)


async def _principal_names(tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    names: dict[uuid.UUID, str] = {}
    if not ids:
        return names
    async with tenant_scope(tenant_id) as session:
        for pid, name in await session.execute(
            select(Persona.principal_id, Persona.name).where(Persona.principal_id.in_(ids))
        ):
            names[pid] = name
        unresolved = ids - set(names)
        if unresolved:
            for pid, name in await session.execute(
                select(Principal.id, Principal.display_name).where(Principal.id.in_(unresolved))
            ):
                names.setdefault(pid, name)
    return names


async def _persona_names(tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    async with tenant_scope(tenant_id) as session:
        rows = await session.execute(select(Persona.id, Persona.name).where(Persona.id.in_(ids)))
        return {pid: name for pid, name in rows}
