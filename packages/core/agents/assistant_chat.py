"""The assistant chat: a session-less, streaming, tool-calling conversation.

The floating widget's backend. Unlike a live-session turn (``run_agent_turn``), nothing
here persists a transcript -- history lives in the caller's browser and arrives with
every request. The assistant can *read* workspace data through viewer-safe service
calls, and can *propose* any edit the UI offers -- but it never executes a write:
a write tool records a ``ProposedAction`` that streams to the UI as a card, and the
user's **Apply** click calls the ordinary API endpoint from their own authenticated
browser session. The assistant's effective access is therefore exactly the asking
user's, by construction, with no new permission surface (and no new INV-5/INV-8
exposure: reads go through the same services the UI uses).

Streaming: ``chat()`` is an async iterator of small event dicts (text deltas, tool
markers, proposals, done/error) that the API layer writes out as NDJSON -- the fix for
"I asked and it never responded": the first token arrives as soon as the model produces
it, and an overall ``asyncio.timeout`` turns a wedged provider into a visible error
event instead of an eternally pending request.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.agents.assistant import (
    _CONTEXT_MAX_TOKENS_SETTING,
    _DEFAULT_CONTEXT_MAX_TOKENS,
    _class_ratios,
    _workspace_context,
    ensure_workspace_assistant,
)
from core.agents.assistant_reads import ViewerGate, refused, register_viewer_read_tools
from core.agents.authoring import (
    list_agents,
    list_personas,
    resolve_connection_api_key,
)
from core.agents.models import Agent
from core.agents.tools import ToolContext, ToolRegistry, ToolResult
from core.audit.models import UsageRecordRow
from core.docs.tools import docs_prompt_line, register_docs_tools
from core.knowledge.authoring import list_draft_entries, list_sources
from core.ports.embedding import EmbeddingProvider
from core.ports.encryptor import Encryptor
from core.ports.model_provider import (
    GenerationRequest,
    ModelProvider,
    ToolCall,
    ToolSpec,
)
from core.ports.permission import PermissionService
from core.repos.service import list_repos
from core.sessions.lifecycle import list_sessions
from core.settings.resolve import resolved_setting
from core.tenancy.egress import load_egress_policy
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from core.workflows.service import list_workflows_for_tenant

log = structlog.get_logger()

_MAX_TOOL_ITERATIONS = 12

# A chat reply is short; a proposal is not. The write tools carry whole documents -- an
# entry's body, a persona's prose, an eight-phase flow -- and 900 tokens, which is a
# generous answer, truncates a flow document into nothing: the tool call never closes, so
# the turn produces neither text nor a proposal and the user sees an empty reply. Sized to
# the largest thing the catalog can be asked to write, not to the common case.
_MAX_REPLY_TOKENS = 8000
_CHAT_TIMEOUT_S = 300
_MAX_HISTORY_MESSAGES = 30

_CHAT_SYSTEM = (
    "You are chatting with a human running this workspace. Answer questions using the "
    "workspace knowledge and the read tools. When the user asks you to change "
    "something, call the matching write tool with complete arguments -- the change is "
    "then SHOWN TO THE USER FOR CONFIRMATION, never applied by you directly; tell the "
    "user you have proposed it and what it contains. Use tools only when they help; "
    "answer directly otherwise. Be concise. Before proposing something destructive or "
    "hard to undo (archive_*, remove_*, delete_*, stop_preview, advance_clock, "
    "transition_entity) name the exact target and say what cannot be undone. Never ask "
    "the user for, or put in a proposal, a token, password or API key -- those are "
    "entered in the UI afterwards."
)


@dataclass
class ProposedAction:
    action: str
    args: dict[str, Any]
    summary: str


@dataclass
class _ChatState:
    proposals: list[ProposedAction] = field(default_factory=list)


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def _s(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _phase_keys(definition: object) -> list[str]:
    """The phase names of a stored flow document, for the assistant to name one."""
    phases = definition.get("phases") if isinstance(definition, dict) else None
    return sorted(str(key) for key in phases) if isinstance(phases, dict) else []


def _register_read_tools(
    registry: ToolRegistry, tenant_id: uuid.UUID, workspace_id: uuid.UUID, gate: ViewerGate
) -> None:
    """Read tools execute inline: they call the same core services the UI's GET routes
    use, so they can never show the assistant more than the UI would show the user."""

    async def _list_personas(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        rows = await list_personas(tenant_id, workspace_id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "id": str(r.id),
                        "key": r.key,
                        "name": r.name,
                        "type": r.persona_type,
                        "persona_md": r.persona_md[:400],
                    }
                    for r in rows
                ]
            )
        )

    async def _list_profiles(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_agents(tenant_id)
        return ToolResult(
            content=json.dumps(
                [
                    {"id": str(r.id), "name": r.name, "provider": r.provider, "model": r.model}
                    for r in rows
                ]
            )
        )

    async def _list_sources(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_sources(tenant_id)
        return ToolResult(
            content=json.dumps(
                [{"id": str(r.id), "key": r.key, "name": r.name, "class": r.class_} for r in rows]
            )
        )

    async def _list_entries(args: dict[str, object], _c: ToolContext) -> ToolResult:
        source_key = str(args.get("source_key", ""))
        source = next((s for s in await list_sources(tenant_id) if s.key == source_key), None)
        if source is None:
            return ToolResult(content=f"no knowledge source with key {source_key!r}")
        rows = await list_draft_entries(tenant_id, source.id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "entry_key": r.entry_key,
                        "title": r.title,
                        "class": r.class_,
                        "body_md": r.body_md[:600],
                    }
                    for r in rows
                ]
            )
        )

    async def _list_sessions(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        rows = await list_sessions(tenant_id, workspace_id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "id": str(r.id),
                        "name": r.name,
                        "status": r.status,
                        "phase": r.current_phase,
                        "turn_policy": r.turn_policy,
                        "agenda_md": (r.agenda_md or "")[:200],
                    }
                    for r in rows[:20]
                ]
            )
        )

    async def _list_repos(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_repos(tenant_id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "id": str(r.id),
                        "key": r.key,
                        "name": r.name,
                        "runtime": r.runtime,
                        "source_url": r.source_url,
                    }
                    for r in rows
                ]
            )
        )

    async def _list_definitions(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        from core.process.authoring import list_definitions

        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        rows = await list_definitions(tenant_id, workspace_id=workspace_id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "id": str(r.id),
                        "key": r.key,
                        "name": r.name,
                        "version": r.version,
                        "phases": _phase_keys(r.definition),
                    }
                    for r in rows
                ]
            )
        )

    async def _get_definition(args: dict[str, object], _c: ToolContext) -> ToolResult:
        from core.process.authoring import list_definitions

        if not await gate.allowed("view_workspace"):
            return refused("view_workspace")
        wanted = str(args.get("key") or "")
        rows = await list_definitions(tenant_id, workspace_id=workspace_id)
        row = next((r for r in rows if r.key == wanted), None)
        if row is None:
            return ToolResult(content=json.dumps({"error": f"no flow {wanted!r}"}))
        return ToolResult(content=json.dumps({"key": row.key, "definition": row.definition}))

    async def _list_workflows(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_workflows_for_tenant(tenant_id)
        return ToolResult(
            content=json.dumps(
                [{"key": r.key, "name": r.name, "is_system": r.tenant_id is None} for r in rows]
            )
        )

    registry.register(
        ToolSpec(
            name="list_personas",
            description="List this workspace's agent personas.",
            parameters=_obj({}, []),
        ),
        _list_personas,
    )
    registry.register(
        ToolSpec(
            name="list_model_profiles",
            description="List the tenant's model connections (provider/model).",
            parameters=_obj({}, []),
        ),
        _list_profiles,
    )
    registry.register(
        ToolSpec(
            name="list_knowledge_sources",
            description="List knowledge sources (rulebooks, handbooks, briefs).",
            parameters=_obj({}, []),
        ),
        _list_sources,
    )
    registry.register(
        ToolSpec(
            name="list_knowledge_entries",
            description="List the entries of one knowledge source by its key.",
            parameters=_obj({"source_key": _s("the knowledge source's key")}, ["source_key"]),
        ),
        _list_entries,
    )
    registry.register(
        ToolSpec(
            name="list_sessions",
            description="List this workspace's sessions.",
            parameters=_obj({}, []),
        ),
        _list_sessions,
    )
    registry.register(
        ToolSpec(
            name="list_repos",
            description="List the registered code repositories.",
            parameters=_obj({}, []),
        ),
        _list_repos,
    )
    registry.register(
        ToolSpec(
            name="list_workflows", description="List available workflows.", parameters=_obj({}, [])
        ),
        _list_workflows,
    )
    registry.register(
        ToolSpec(
            name="get_process_definition",
            description=(
                "Read one flow's whole DSL document by key -- the phases, actors, "
                "visibility, budgets and transitions. Read an existing flow before "
                "authoring one."
            ),
            parameters=_obj({"key": _s("the flow's key (from list_process_definitions)")}, ["key"]),
        ),
        _get_definition,
    )
    registry.register(
        ToolSpec(
            name="list_process_definitions",
            description="List the flows (process definitions) this workspace can run.",
            parameters=_obj({}, []),
        ),
        _list_definitions,
    )


# One entry per write tool: (description, JSON-schema params). Args mirror the request
# bodies of the API endpoints the UI's Apply step calls -- the frontend's action map is
# the executable counterpart of this table.
_WRITE_TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "update_persona": (
        "Update an existing persona (name, persona_md prose, persona_type, web_search).",
        _obj(
            {
                "persona_id": _s("the persona's id (from list_personas)"),
                "name": _s("new display name (optional)"),
                "persona_md": _s("full replacement persona prose (optional)"),
                "persona_type": _s("supervisor | participant | informational (optional)"),
            },
            ["persona_id"],
        ),
    ),
    "create_persona": (
        "Create a new persona in this workspace.",
        _obj(
            {
                "key": _s("stable slug"),
                "name": _s("display name"),
                "persona_md": _s("persona prose"),
                "persona_type": _s("supervisor | participant"),
                "agent_id": _s("model connection id (from list_model_profiles)"),
            },
            ["key", "name", "agent_id"],
        ),
    ),
    "archive_persona": (
        "Archive (soft-delete) a persona.",
        _obj({"persona_id": _s("the persona's id")}, ["persona_id"]),
    ),
    "upsert_knowledge_entry": (
        "Create or replace one entry in a knowledge source.",
        _obj(
            {
                "source_key": _s("knowledge source key (from list_knowledge_sources)"),
                "entry_key": _s("entry key (new or existing)"),
                "title": _s("entry title"),
                "body_md": _s("full markdown body"),
                "class": _s("rules | lore | misc"),
                "scope_key": _s(
                    "who may read it: workspace_public (default, everyone) or "
                    "facilitator_only (the referee/supervisor alone)"
                ),
                "constant": _s(
                    "'true' to put this entry in EVERY turn regardless of the "
                    "conversation. Reserve it for the one or two things that must "
                    "always be present; everything else should be retrieved."
                ),
                "keys": _s(
                    "comma-separated words that should activate this entry when they "
                    "appear in a turn. Leave empty for a rules entry and the title is "
                    "used."
                ),
                "insertion_order": _s("author priority among always-on entries (lower first)"),
            },
            ["source_key", "entry_key", "title", "body_md"],
        ),
    ),
    "attach_knowledge_source": (
        "Attach a knowledge source to this workspace so its entries can be retrieved. "
        "A source that is created and published but never attached reaches nobody.",
        _obj(
            {
                "source_key": _s("knowledge source key"),
                "scope_key": _s("scope the attachment reads under (default workspace_public)"),
            },
            ["source_key"],
        ),
    ),
    "create_process_definition": (
        "Author a flow (process definition): its phases, actors, visibility, budgets and "
        "transitions, as the DSL document. Validated on apply; invalid documents are "
        "refused with the reason.",
        _obj(
            {
                "key": _s("stable slug"),
                "name": _s("display name"),
                "definition": {
                    "type": "object",
                    "description": (
                        "the whole DSL document: name, vocabulary_overlay, initial_phase, "
                        "phases{...}, optional state"
                    ),
                },
            },
            ["key", "name", "definition"],
        ),
    ),
    "create_session": (
        "Start a session on a flow, with a supervisor persona and participants.",
        _obj(
            {
                "process_definition_id": _s("flow id (from list_process_definitions)"),
                "supervisor_persona_id": _s("the conducting persona's id"),
                "participant_persona_ids": _s("comma-separated persona ids"),
                "name": _s("session display name"),
                "agenda_md": _s("what this session is for"),
                "turn_policy": _s("auto | directed (default auto)"),
            },
            ["process_definition_id", "supervisor_persona_id"],
        ),
    ),
    "create_knowledge_source": (
        "Create a new knowledge source (rulebook/handbook).",
        _obj(
            {
                "key": _s("stable slug"),
                "name": _s("display name"),
                "class": _s("rules | lore | misc"),
            },
            ["key", "name"],
        ),
    ),
    "publish_knowledge_source": (
        "Publish a knowledge source's current draft entries as a new version.",
        _obj(
            {"source_key": _s("knowledge source key"), "change_note": _s("what changed")},
            ["source_key"],
        ),
    ),
    "rename_session": (
        "Rename a session.",
        _obj(
            {"session_id": _s("session id (from list_sessions)"), "name": _s("new display name")},
            ["session_id", "name"],
        ),
    ),
    "set_session_agenda": (
        "Set or replace a session's agenda.",
        _obj(
            {"session_id": _s("session id"), "agenda_md": _s("full agenda markdown")},
            ["session_id", "agenda_md"],
        ),
    ),
    "set_turn_policy": (
        "Switch a session between autonomous and managed turn policy.",
        _obj(
            {"session_id": _s("session id"), "turn_policy": _s("auto | directed")},
            ["session_id", "turn_policy"],
        ),
    ),
    "update_workflow": (
        "Update a tenant-authored workflow (name, label overrides, featured flows).",
        _obj(
            {
                "key": _s("workflow key (from list_workflows; not a system one)"),
                "name": _s("new name (optional)"),
            },
            ["key"],
        ),
    ),
    "set_current_workflow": (
        "Switch the tenant's active workflow.",
        _obj({"workflow_key": _s("workflow key")}, ["workflow_key"]),
    ),
    "update_repo": (
        "Update a registered repository (name, remote URL, runtime, setup/test commands).",
        _obj(
            {
                "repo_id": _s("repo id (from list_repos)"),
                "name": _s("new name (optional)"),
                "source_url": _s("remote https URL (optional)"),
                "runtime": _s("debian | node20 | python312 | java21 (optional)"),
                "test_cmd": _s("test command (optional)"),
            },
            ["repo_id"],
        ),
    ),
    "create_model_profile": (
        "Create a model connection.",
        _obj(
            {
                "name": _s("display name"),
                "provider": _s("ollama | openai | anthropic | gemini"),
                "model": _s("model name"),
                "api_base": _s("endpoint URL (optional)"),
            },
            ["name", "provider", "model"],
        ),
    ),
    "update_model_profile": (
        "Update a model connection (never its stored key).",
        _obj(
            {
                "profile_id": _s("connection id (from list_model_profiles)"),
                "name": _s("new name (optional)"),
                "model": _s("new model (optional)"),
                "api_base": _s("new endpoint URL (optional)"),
            },
            ["profile_id"],
        ),
    ),
    # ── secrets and entities ──────────────────────────────────────────────────────
    "create_secret": (
        "Create a secret held by a character or record. Prefer typing sensitive content "
        "in the Secrets page yourself; what you write here travels through the model.",
        _obj(
            {
                "subject_kind": _s("entity | agent | workspace | knowledge_entry"),
                "subject_id": _s("id of the subject (an entity id, or a persona's agent id)"),
                "content": _s("the secret itself (plaintext)"),
                "gist": _s("a one-line gist others may see"),
                "scope_key": _s("scope key (from get_workspace visibility), e.g. workspace_public"),
                "hint_text": _s("what a hint may reveal (optional)"),
                "behavioral_directive": _s("how the holder behaves about it (optional)"),
                "publication": _s("guarded | publishable (default guarded)"),
            },
            ["subject_kind", "subject_id", "content", "gist", "scope_key"],
        ),
    ),
    "update_secret": (
        "Update a secret's content, gist, hint, directive or publication. Fields you omit "
        "are left unchanged; clear_hint / clear_directive remove those two.",
        _obj(
            {
                "secret_id": _s("secret id (from list_secrets)"),
                "content": _s("new plaintext (optional)"),
                "gist": _s("new gist (optional)"),
                "hint_text": _s("new hint (optional)"),
                "behavioral_directive": _s("new directive (optional)"),
                "publication": _s("guarded | publishable (optional)"),
                "clear_hint": _s("'true' to remove the hint"),
                "clear_directive": _s("'true' to remove the directive"),
            },
            ["secret_id"],
        ),
    ),
    "add_secret_holder": (
        "Give a persona or human knowledge of a secret (they become a holder).",
        _obj(
            {
                "secret_id": _s("secret id (from list_secrets)"),
                "holder_principal_id": _s("principal id (from get_workspace members)"),
                "holder_kind": _s("told | witnessed | author"),
            },
            ["secret_id", "holder_principal_id", "holder_kind"],
        ),
    ),
    "remove_secret_holder": (
        "Remove a holder from a secret.",
        _obj(
            {
                "secret_id": _s("secret id"),
                "holder_id": _s("holder id (from list_secret_holders)"),
            },
            ["secret_id", "holder_id"],
        ),
    ),
    "create_entity_schema": (
        "Create an entity schema, or a new version of an existing key: fields, derived "
        "values, views and state machines, as the definition document (read the current "
        "one with get_entity_schema first). Validated on apply.",
        _obj(
            {
                "key": _s("schema key"),
                "definition": {"type": "object", "description": "the schema definition"},
            },
            ["key", "definition"],
        ),
    ),
    "transition_entity": (
        "Move an entity along one of its state machines by firing a trigger (see the "
        "transitions get_entity lists).",
        _obj(
            {
                "entity_id": _s("entity id"),
                "trigger": _s("transition trigger"),
                "machine_key": _s("state machine key (default lifecycle)"),
                "expected_version": _s("the entity version you read (optional)"),
            },
            ["entity_id", "trigger"],
        ),
    ),
    # ── the workspace ─────────────────────────────────────────────────────────────
    "update_workspace_settings": (
        "Change this workspace's settings: secret_mode (excluded | trust | gate), "
        "conduct_rules, allow_automerge, max_review_rounds, moderation_model, "
        "assistant_context_max_tokens. Omitted fields stay; clear_* flags unset one.",
        _obj(
            {
                "secret_mode": _s("excluded | trust | gate (optional)"),
                "conduct_rules": _s("conduct rules text (optional)"),
                "allow_automerge": _s("'true' | 'false' (optional)"),
                "max_review_rounds": _s("integer (optional)"),
                "moderation_model": _s("model profile id for moderation (optional)"),
                "assistant_context_max_tokens": _s("integer budget (optional)"),
                "clear_max_review_rounds": _s("'true' to inherit the default"),
                "clear_moderation_model": _s("'true' to inherit the default"),
                "clear_assistant_context_max_tokens": _s("'true' to inherit the default"),
            },
            [],
        ),
    ),
    "add_workspace_member": (
        "Add a person to this workspace by email, with a workspace role.",
        _obj(
            {
                "email": _s("the person's login email"),
                "role": _s(
                    "facilitator | participant | viewer | overseer | steward (default participant)"
                ),
            },
            ["email"],
        ),
    ),
    "remove_workspace_member": (
        "Remove a human member from this workspace.",
        _obj({"principal_id": _s("principal id (from get_workspace members)")}, ["principal_id"]),
    ),
    "set_persona_scopes": (
        "Set which group scopes a persona may read (its levels of lore).",
        _obj(
            {
                "persona_id": _s("persona id (from list_personas)"),
                "scopes": _s("comma-separated scope keys"),
            },
            ["persona_id", "scopes"],
        ),
    ),
    "advance_clock": (
        "Advance the workspace clock to a value (between-session time passing).",
        _obj({"to_value": _s("the new clock value (integer)")}, ["to_value"]),
    ),
    "create_workspace": (
        "Create a new workspace in this organization.",
        _obj({"name": _s("display name"), "key": _s("short key (optional)")}, ["name"]),
    ),
    "archive_workspace": (
        "Archive a workspace (it disappears from lists; sessions in it stop).",
        _obj({"workspace_id": _s("workspace id (from list_workspaces)")}, ["workspace_id"]),
    ),
    # ── sessions ──────────────────────────────────────────────────────────────────
    "delegate_work": (
        "Hand work items to the coding agents: each becomes a branch and a pull request.",
        _obj(
            {
                "session_id": _s("session id"),
                "work_item_ids": _s("comma-separated entity ids of the work items"),
                "repo_id": _s("repository id (optional; the session's default otherwise)"),
                "auto_review": _s("'false' to skip the automatic review (default true)"),
            },
            ["session_id", "work_item_ids"],
        ),
    ),
    "request_review_changes": (
        "Send a pull request back for rework with a comment.",
        _obj(
            {
                "session_id": _s("session id"),
                "work_item_id": _s("the work item's entity id"),
                "branch": _s("the branch under review"),
                "comment": _s("what to change (optional)"),
                "repo_id": _s("repository id (optional)"),
            },
            ["session_id", "work_item_id", "branch"],
        ),
    ),
    "pause_session": (
        "Pause a running session.",
        _obj({"session_id": _s("session id")}, ["session_id"]),
    ),
    "resume_session": (
        "Resume a paused session.",
        _obj({"session_id": _s("session id")}, ["session_id"]),
    ),
    "wrap_up_session": (
        "Ask a managed session's supervisor to wrap up and synthesise.",
        _obj({"session_id": _s("session id")}, ["session_id"]),
    ),
    "direct_turn": (
        "In a managed session, have one persona take the next turn.",
        _obj(
            {"session_id": _s("session id"), "persona_id": _s("persona id")},
            ["session_id", "persona_id"],
        ),
    ),
    "continue_session": (
        "Run more autonomous rounds on a finished session.",
        _obj({"session_id": _s("session id"), "rounds": _s("1..10 (default 1)")}, ["session_id"]),
    ),
    "generate_report": (
        "Render a report for a session (templates from list_report_templates).",
        _obj(
            {"session_id": _s("session id"), "template_key": _s("template key")},
            ["session_id", "template_key"],
        ),
    ),
    "request_recap": (
        "Write a recap of a session for the asking user.",
        _obj({"session_id": _s("session id")}, ["session_id"]),
    ),
    "archive_session": (
        "Archive a session (it leaves the list; its record is kept).",
        _obj({"session_id": _s("session id")}, ["session_id"]),
    ),
    # ── knowledge and vocabulary ──────────────────────────────────────────────────
    "archive_knowledge_source": (
        "Archive a knowledge source (its entries stop being retrieved).",
        _obj({"source_key": _s("source key (from list_knowledge_sources)")}, ["source_key"]),
    ),
    "set_workspace_vocabulary": (
        "Choose which vocabulary overlay this workspace is labelled with.",
        _obj(
            {
                "overlay_key": _s(
                    "overlay key (from get_tenant_config); empty = inherit the default"
                )
            },
            [],
        ),
    ),
    "set_tenant_default_vocabulary": (
        "Choose the organization's default vocabulary overlay.",
        _obj({"overlay_key": _s("overlay key; empty = the system default")}, []),
    ),
    # ── repositories, runtimes and the engine (repo:manage) ───────────────────────
    "register_repo": (
        "Register a code repository for delegated work. Never carries a token: the user "
        "adds the access token under Repos afterwards.",
        _obj(
            {
                "key": _s("short key"),
                "name": _s("display name"),
                "description": _s("what it is (optional)"),
                "source_url": _s("clone URL (optional)"),
                "provider": _s("github | gitlab | gitea | generic (optional)"),
                "runtime": _s("build runtime key (from get_tenant_config; default debian)"),
                "runtime_image": _s("a custom image instead of a runtime (optional)"),
                "setup_cmds": _s("comma-separated setup commands (optional)"),
                "test_cmd": _s("test command (optional)"),
                "build_cmd": _s("build command (optional)"),
                "artifact_name": _s("build artifact file name (optional)"),
                "preview_cmd": _s("preview command (optional)"),
                "preview_port": _s("preview port (optional)"),
            },
            ["key", "name"],
        ),
    ),
    "put_runtime": (
        "Create or update a build runtime (an image plus setup commands).",
        _obj(
            {
                "key": _s("runtime key"),
                "image": _s("container image reference"),
                "setup": _s("comma-separated setup commands (optional)"),
            },
            ["key", "image"],
        ),
    ),
    "delete_runtime": (
        "Delete a tenant-defined build runtime.",
        _obj({"key": _s("runtime key")}, ["key"]),
    ),
    "refresh_repo": (
        "Fetch a repository's remote again.",
        _obj({"repo_id": _s("repository id (from list_repos)")}, ["repo_id"]),
    ),
    "archive_repo": (
        "Archive a repository registration.",
        _obj({"repo_id": _s("repository id (from list_repos)")}, ["repo_id"]),
    ),
    "set_exec_engine": (
        "Choose which execution engine delegated work runs on.",
        _obj({"engine": _s("engine key (from get_tenant_config)")}, ["engine"]),
    ),
    # ── MCP servers (workflow:manage) ─────────────────────────────────────────────
    "upsert_mcp_server": (
        "Attach or update an MCP server for this workspace. Credentials are not part of "
        "this: the user sets a credential reference in the MCP page afterwards.",
        _obj(
            {
                "key": _s("server key"),
                "url": _s("server URL"),
                "enabled_tools": _s("comma-separated tool names the personas may call"),
                "effectful_tools": _s("comma-separated tools that change things (optional)"),
                "require_confirmation": _s("'false' to skip confirmation (default true)"),
                "max_calls_per_session": _s("integer cap (optional)"),
                "timeout_seconds": _s("integer (optional)"),
                "max_result_chars": _s("integer (optional)"),
            },
            ["key", "url", "enabled_tools"],
        ),
    ),
    "delete_mcp_server": (
        "Detach an MCP server from this workspace.",
        _obj({"key": _s("server key")}, ["key"]),
    ),
    # ── the organization (manage_tenant) ──────────────────────────────────────────
    "set_limits": (
        "Set the organization's daily token caps (0 = unlimited).",
        _obj(
            {
                "tenant_daily_tokens": _s("integer"),
                "per_connection_daily_tokens": _s("integer"),
                "per_persona_daily_tokens": _s("integer"),
                "per_user_daily_tokens": _s("integer"),
            },
            [],
        ),
    ),
    "set_tenant_settings": (
        "Set organization preferences: login lifetime, preview lifetime, reranking.",
        _obj(
            {
                "session_lifetime_seconds": _s("integer (optional)"),
                "preview_ttl_seconds": _s("integer (optional)"),
                "reranker_enabled": _s("'true' | 'false' (optional)"),
            },
            [],
        ),
    ),
    "deploy_preview": (
        "Run a repository's latest build as a preview with a share link.",
        _obj(
            {
                "repo_id": _s("repository id"),
                "session_id": _s("session to attach it to (optional)"),
                "ttl_seconds": _s("lifetime in seconds (optional)"),
                "git_ref": _s("branch or ref (optional; the latest build otherwise)"),
            },
            ["repo_id"],
        ),
    ),
    "stop_preview": (
        "Stop a running preview.",
        _obj({"preview_id": _s("preview id (from get_tenant_config)")}, ["preview_id"]),
    ),
    "request_export": (
        "Export this workspace as a .pyr bundle (participant or sanitised mode; the full "
        "mode and model connections need a password and are done in the Export page).",
        _obj(
            {
                "mode": _s("participant | sanitised (default participant)"),
                "sections": _s("comma-separated sections to include (optional; all otherwise)"),
            },
            [],
        ),
    ),
}

# Tools an ordinary member could never Apply are not offered to them: the viewer's own
# tenant permission, asked at registration, decides. (Workspace-level tools register for
# everyone -- the Apply call's own 403 is the backstop there.)
_WRITE_TOOL_GATES: dict[str, str] = {
    "register_repo": "repo:manage",
    "put_runtime": "repo:manage",
    "delete_runtime": "repo:manage",
    "refresh_repo": "repo:manage",
    "archive_repo": "repo:manage",
    "set_exec_engine": "repo:manage",
    "update_repo": "repo:manage",
    "deploy_preview": "repo:manage",
    "stop_preview": "repo:manage",
    "upsert_mcp_server": "workflow:manage",
    "delete_mcp_server": "workflow:manage",
    "set_limits": "manage_tenant",
    "set_tenant_settings": "manage_tenant",
    "create_workspace": "manage_tenant",
}


# Secret plaintext a user dictated is for the Apply call, not for the summary the model
# reads back; hints and directives are secret material too.
_UNSUMMARISED = frozenset({"content", "hint_text", "behavioral_directive"})


async def _register_write_tools(
    registry: ToolRegistry, state: _ChatState, gate: ViewerGate
) -> None:
    """Write tools NEVER execute: the handler records a proposal for the UI. The model
    is told so in its result, so it reports the proposal instead of claiming success."""
    for action, (description, parameters) in _WRITE_TOOLS.items():
        required = _WRITE_TOOL_GATES.get(action)
        if required and not await gate.allowed(required, tenant_level=True):
            continue

        async def _propose(
            args: dict[str, object],
            _c: ToolContext,
            *,
            _action: str = action,
            _declared: frozenset[str] = frozenset(parameters.get("properties", {})),
        ) -> ToolResult:
            # Only the declared parameters travel: a stray token, password or key the
            # model invents for a tool that never asked for one is dropped here, before
            # the proposal reaches the browser.
            kept = {k: v for k, v in args.items() if k in _declared and v not in (None, "")}
            summary_bits = ", ".join(
                f"{k}={'(hidden)' if k in _UNSUMMARISED else str(v)[:60]}" for k, v in kept.items()
            )
            state.proposals.append(
                ProposedAction(action=_action, args=kept, summary=f"{_action}({summary_bits})")
            )
            return ToolResult(
                content=(
                    f"Proposed '{_action}' to the user -- it is shown in the chat with "
                    "an Apply button and takes effect only if they confirm. Tell the "
                    "user what you proposed."
                )
            )

        registry.register(
            ToolSpec(
                name=action,
                description=f"{description} PROPOSAL ONLY: the user confirms in the UI.",
                parameters=parameters,
            ),
            _propose,
        )


async def chat(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    messages: list[dict[str, str]],
    *,
    embedder: EmbeddingProvider,
    provider_factory: Any,  # Callable[[str], ModelProvider] -- composition root's
    encryptor: Encryptor,
    permission_service: PermissionService,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one assistant reply (plus any proposals) for the given history."""
    try:
        async with asyncio.timeout(_CHAT_TIMEOUT_S):
            async for event in _chat_inner(
                tenant_id,
                workspace_id,
                viewer,
                messages,
                embedder=embedder,
                provider_factory=provider_factory,
                encryptor=encryptor,
                permission_service=permission_service,
            ):
                yield event
    except TimeoutError:
        yield {"type": "error", "detail": f"assistant timed out after {_CHAT_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001 -- the stream is the error channel
        log.warning("assistant_chat.failed", error=str(exc))
        yield {"type": "error", "detail": str(exc)[:300]}


async def _chat_inner(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    messages: list[dict[str, str]],
    *,
    embedder: EmbeddingProvider,
    provider_factory: Any,
    encryptor: Encryptor,
    permission_service: PermissionService,
) -> AsyncIterator[dict[str, Any]]:
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    async with tenant_scope(tenant_id) as session:
        profile = await session.get(Agent, persona.agent_id)
        if profile is None:
            raise ValueError("the assistant's model profile is missing")
        if not profile.model:
            # Fresh install, no provider configured yet -- name the fix rather than
            # letting the stream die on a connection error to a host nobody set up.
            raise ValueError(
                "no model is configured for the assistant yet -- set one on the "
                "'Assistant model' profile under Personas"
            )
        session.expunge(profile)
    from core.usage_limits import ensure_within_limits

    await ensure_within_limits(
        tenant_id, agent_id=profile.id, persona_id=persona.id, principal_id=viewer.id
    )
    provider: ModelProvider = provider_factory(profile.provider)
    api_key = await resolve_connection_api_key(
        tenant_id, profile.credential_ref, encryptor=encryptor
    )

    history: list[dict[str, object]] = [
        {"role": m["role"], "content": m["content"]}
        for m in messages[-_MAX_HISTORY_MESSAGES:]
        if m.get("role") in ("user", "assistant", "system") and m.get("content")
    ]
    last_user = str(next((m["content"] for m in reversed(history) if m["role"] == "user"), ""))
    # Same budget the one-shot assist path resolves: the chat widget and /assist are the
    # same assistant reading the same workspace, and a budget that applied to one of them
    # would be a setting the user could only half-see the effect of.
    context, entry_keys = await _workspace_context(
        tenant_id,
        workspace_id,
        viewer,
        last_user,
        embedder,
        int(
            await resolved_setting(
                tenant_id,
                workspace_id,
                _CONTEXT_MAX_TOKENS_SETTING,
                _DEFAULT_CONTEXT_MAX_TOKENS,
            )
        ),
        await _class_ratios(tenant_id, workspace_id),
    )

    state = _ChatState()
    registry = ToolRegistry()
    gate = ViewerGate(permission_service, tenant_id, viewer.id, workspace_id)
    _register_read_tools(registry, tenant_id, workspace_id, gate)
    register_viewer_read_tools(
        registry,
        tenant_id,
        workspace_id,
        viewer,
        encryptor=encryptor,
        permission_service=permission_service,
    )
    await _register_write_tools(registry, state, gate)
    register_docs_tools(registry)

    system = f"{persona.persona_md}\n\n{_CHAT_SYSTEM}\n\n{docs_prompt_line()}"
    if context:
        system += f"\n\nWorkspace knowledge (viewer-scoped):\n{context}"
    conversation: list[dict[str, object]] = [
        {"role": "system", "content": system},
        *history,
    ]

    model_string = f"{profile.provider}/{profile.model}"
    tool_ctx = ToolContext(tenant_id=tenant_id, persona_id=persona.id, session_id=None)

    for _iteration in range(_MAX_TOOL_ITERATIONS):
        req = GenerationRequest(
            egress_policy=await load_egress_policy(tenant_id),
            model=model_string,
            messages=conversation,
            purpose="generation",
            max_tokens=_MAX_REPLY_TOKENS,
            tools=registry.specs(),
            api_base=profile.api_base,
            params=dict(profile.params or {}),
            api_key=api_key,
        )
        pieces: list[str] = []
        tool_calls: tuple[ToolCall, ...] = ()
        start = time.monotonic()
        async for chunk in provider.generate(req):
            if chunk.text:
                pieces.append(chunk.text)
                yield {"type": "text", "delta": chunk.text}
            if chunk.tool_calls:
                tool_calls = chunk.tool_calls
        content = "".join(pieces)

        async with tenant_scope(tenant_id) as session:
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    persona_id=persona.id,
                    agent_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    purpose="generation",
                    principal_id=viewer.id,
                    prompt_tokens=sum(
                        provider.count_tokens(str(m.get("content") or ""), model_string)
                        for m in conversation
                    ),
                    completion_tokens=provider.count_tokens(content, model_string),
                    latency_ms=int((time.monotonic() - start) * 1000),
                )
            )

        if not tool_calls:
            yield {"type": "done", "context_entry_keys": entry_keys}
            return

        # The assistant turn that REQUESTED the tools must carry tool_calls back in the
        # transcript, in the OpenAI shape -- the same requirement core/agents/runtime.py
        # already honours for session turns. Without it every provider that validates the
        # pairing rejects the NEXT request outright ("Messages with role 'tool' must be a
        # response to a preceding message with 'tool_calls'"), so the assistant could call
        # one tool and then die instead of answering.
        conversation.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tool_call in tool_calls:
            yield {"type": "tool", "name": tool_call.name}
            before = len(state.proposals)
            try:
                result = await registry.dispatch(tool_call, tool_ctx)
                result_content = result.content
            except Exception as exc:  # noqa: BLE001 -- feed the failure back to the model
                result_content = f"tool {tool_call.name} failed: {str(exc)[:300]}"
            for proposal in state.proposals[before:]:
                yield {
                    "type": "proposal",
                    "action": proposal.action,
                    "args": proposal.args,
                    "summary": proposal.summary,
                }
            conversation.append(
                {
                    "role": "tool",
                    "content": result_content,
                    "tool_call_id": tool_call.id,
                }
            )

    yield {"type": "error", "detail": f"tool loop exceeded {_MAX_TOOL_ITERATIONS} iterations"}
