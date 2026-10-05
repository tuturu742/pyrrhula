import { apiClient } from "@/lib/api-client/client";

/**
 * The executable counterpart of the backend's write-tool catalog
 * (`core/agents/assistant_chat.py` `_WRITE_TOOLS`): the assistant only PROPOSES an
 * action; clicking Apply runs the matching ordinary API call from the user's own
 * browser session — so the assistant's effective access is exactly the user's.
 */
export interface ApplyResult {
  ok: boolean;
  detail: string;
}

type Args = Record<string, unknown>;
const s = (v: unknown): string => (typeof v === "string" ? v : String(v ?? ""));
/** A model writes a list as a comma-separated string as often as it writes an array. */
const list = (v: unknown): string[] =>
  Array.isArray(v)
    ? v.map(s).filter(Boolean)
    : s(v)
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean);
const bool = (v: unknown): boolean =>
  v === true || ["true", "yes", "1"].includes(s(v).toLowerCase());
const num = (v: unknown): number => {
  const parsed = Number.parseInt(s(v), 10);
  return Number.isFinite(parsed) ? parsed : 0;
};

async function sourceIdByKey(key: string): Promise<string | null> {
  const { data } = await apiClient.GET("/knowledge/sources");
  return data?.find((src) => src.key === key)?.id ?? null;
}

async function overlayIdByKey(key: string): Promise<string | null> {
  const { data } = await apiClient.GET("/vocabulary-overlays");
  return data?.find((o) => o.key === key)?.id ?? null;
}

function result(error: unknown, okDetail: string): ApplyResult {
  if (error) {
    const detail =
      typeof error === "object" && error !== null && "detail" in error
        ? String((error as { detail: unknown }).detail)
        : "request failed";
    return { ok: false, detail };
  }
  return { ok: true, detail: okDetail };
}

export async function applyAssistantAction(
  action: string,
  args: Args,
  workspaceId: string,
): Promise<ApplyResult> {
  switch (action) {
    case "update_persona": {
      const { error } = await apiClient.PATCH("/agents/{persona_id}", {
        params: { path: { persona_id: s(args.persona_id) } },
        body: {
          name: args.name ? s(args.name) : null,
          persona_md: args.persona_md ? s(args.persona_md) : null,
          persona_type: args.persona_type ? s(args.persona_type) : null,
        },
      });
      return result(error, "Persona updated.");
    }
    case "create_persona": {
      const { error } = await apiClient.POST("/agents", {
        body: {
          workspace_id: workspaceId,
          key: s(args.key),
          name: s(args.name),
          agent_id: s(args.agent_id),
          persona_type: args.persona_type ? s(args.persona_type) : "participant",
          persona_md: s(args.persona_md ?? ""),
          // Not the assistant's to choose: a harness runs a shell in a container, and
          // the only place that is selected is the roster, by a person.
          harness: "",
          web_search: false,
          params: {},
        },
      });
      return result(error, "Persona created.");
    }
    case "archive_persona": {
      const { error } = await apiClient.DELETE("/agents/{persona_id}", {
        params: { path: { persona_id: s(args.persona_id) } },
      });
      return result(error, "Persona archived.");
    }
    case "upsert_knowledge_entry": {
      const sourceId = await sourceIdByKey(s(args.source_key));
      if (!sourceId) return { ok: false, detail: `no source ${s(args.source_key)}` };
      const { error } = await apiClient.PUT(
        "/knowledge/sources/{source_id}/entries/{entry_key}",
        {
          params: { path: { source_id: sourceId, entry_key: s(args.entry_key) } },
          body: {
            title: s(args.title),
            body_md: s(args.body_md),
            class: s(args.class ?? "lore"),
            scope_key: s(args.scope_key || "workspace_public"),
            keys: list(args.keys),
            secondary_keys: [],
            logic: "AND",
            use_regex: false,
            constant: bool(args.constant),
            position: "before_char",
            insertion_order: num(args.insertion_order),
          },
        },
      );
      return result(error, "Entry saved (draft).");
    }
    case "attach_knowledge_source": {
      const sourceId = await sourceIdByKey(s(args.source_key));
      if (!sourceId) return { ok: false, detail: `no source ${s(args.source_key)}` };
      const { error } = await apiClient.POST("/knowledge/sources/{source_id}/attachments", {
        params: { path: { source_id: sourceId } },
        body: {
          workspace_id: workspaceId,
          scope_key: s(args.scope_key || "workspace_public"),
          priority_weight: 1.0,
          enabled: true,
        },
      });
      return result(error, "Source attached to this workspace.");
    }
    case "create_process_definition": {
      const { error } = await apiClient.POST("/process-definitions", {
        body: {
          key: s(args.key),
          name: s(args.name),
          definition: (args.definition ?? {}) as Record<string, never>,
          workspace_id: workspaceId,
        },
      });
      return result(error, "Flow created.");
    }
    case "create_session": {
      const { error } = await apiClient.POST("/sessions", {
        body: {
          workspace_id: workspaceId,
          process_definition_id: s(args.process_definition_id),
          supervisor_persona_id: s(args.supervisor_persona_id),
          participant_persona_ids: list(args.participant_persona_ids),
          name: args.name ? s(args.name) : null,
          agenda_md: args.agenda_md ? s(args.agenda_md) : null,
          turn_policy: (s(args.turn_policy || "auto") === "directed"
            ? "directed"
            : "auto") as "auto" | "directed",
          repo_ids: [],
        },
      });
      return result(error, "Session started.");
    }
    case "create_knowledge_source": {
      const { error } = await apiClient.POST("/knowledge/sources", {
        body: { key: s(args.key), name: s(args.name), class: s(args.class ?? "lore"), visibility: "tenant" },
      });
      return result(error, "Knowledge source created.");
    }
    case "publish_knowledge_source": {
      const sourceId = await sourceIdByKey(s(args.source_key));
      if (!sourceId) return { ok: false, detail: `no source ${s(args.source_key)}` };
      const { error } = await apiClient.POST("/knowledge/sources/{source_id}/publish", {
        params: { path: { source_id: sourceId } },
        body: { change_note: s(args.change_note ?? "assistant-proposed publish") },
      });
      return result(error, "Version published.");
    }
    case "rename_session": {
      const { error } = await apiClient.PATCH("/sessions/{session_id}", {
        params: { path: { session_id: s(args.session_id) } },
        body: { name: s(args.name) },
      });
      return result(error, "Session renamed.");
    }
    case "set_session_agenda": {
      const { error } = await apiClient.PATCH("/sessions/{session_id}/agenda", {
        params: { path: { session_id: s(args.session_id) } },
        body: { agenda_md: s(args.agenda_md) },
      });
      return result(error, "Agenda updated.");
    }
    case "set_turn_policy": {
      const { error } = await apiClient.PATCH("/sessions/{session_id}/turn-policy", {
        params: { path: { session_id: s(args.session_id) } },
        body: { turn_policy: s(args.turn_policy) as "auto" | "directed" },
      });
      return result(error, "Turn policy updated.");
    }
    case "update_workflow": {
      const { error } = await apiClient.PATCH("/workflows/{key}", {
        params: { path: { key: s(args.key) } },
        body: { name: args.name ? s(args.name) : null, clear_overlay: false },
      });
      return result(error, "Workflow updated.");
    }
    case "set_current_workflow": {
      const { error } = await apiClient.PUT("/workflows/current", {
        body: { workflow_key: s(args.workflow_key) },
      });
      return result(error, "Workflow switched.");
    }
    case "update_repo": {
      const { error } = await apiClient.PATCH("/repos/{repo_id}", {
        params: { path: { repo_id: s(args.repo_id) } },
        body: {
          name: args.name ? s(args.name) : null,
          source_url: args.source_url ? s(args.source_url) : null,
          runtime: args.runtime ? s(args.runtime) : null,
          test_cmd: args.test_cmd ? s(args.test_cmd) : null,
          clear_test_cmd: false,
          clear_build: false,
          clear_preview: false,
        },
      });
      return result(error, "Repo updated.");
    }
    case "create_model_profile": {
      const { error } = await apiClient.POST("/model-profiles", {
        body: {
          name: s(args.name),
          provider: s(args.provider),
          model: s(args.model),
          api_base: args.api_base ? s(args.api_base) : null,
          params: {},
        },
      });
      return result(error, "Connection created.");
    }
    case "update_model_profile": {
      const { error } = await apiClient.PATCH("/model-profiles/{agent_id}", {
        params: { path: { agent_id: s(args.profile_id) } },
        body: {
          name: args.name ? s(args.name) : null,
          model: args.model ? s(args.model) : null,
          api_base: args.api_base ? s(args.api_base) : undefined,
        },
      });
      return result(error, "Connection updated.");
    }
    case "create_secret": {
      const { error } = await apiClient.POST("/secrets", {
        body: {
          workspace_id: workspaceId,
          subject_kind: s(args.subject_kind),
          subject_id: s(args.subject_id),
          content: s(args.content),
          gist: s(args.gist),
          scope_key: s(args.scope_key),
          hint_text: args.hint_text ? s(args.hint_text) : null,
          behavioral_directive: args.behavioral_directive ? s(args.behavioral_directive) : null,
          publication: args.publication ? s(args.publication) : "guarded",
        },
      });
      return result(error, "Secret created.");
    }
    case "update_secret": {
      // An omitted field is left as it is (the API's "__unset__" default); only a
      // clear_* flag sends null.
      const body: Record<string, unknown> = {};
      if (args.content) body.content = s(args.content);
      if (args.gist) body.gist = s(args.gist);
      if (args.publication) body.publication = s(args.publication);
      if (bool(args.clear_hint)) body.hint_text = null;
      else if (args.hint_text) body.hint_text = s(args.hint_text);
      if (bool(args.clear_directive)) body.behavioral_directive = null;
      else if (args.behavioral_directive) body.behavioral_directive = s(args.behavioral_directive);
      const { error } = await apiClient.PATCH("/secrets/{secret_id}", {
        params: { path: { secret_id: s(args.secret_id) } },
        body: body as never,
      });
      return result(error, "Secret updated.");
    }
    case "add_secret_holder": {
      const { error } = await apiClient.POST("/secrets/{secret_id}/holders", {
        params: { path: { secret_id: s(args.secret_id) } },
        body: {
          holder_principal_id: s(args.holder_principal_id),
          holder_kind: s(args.holder_kind),
        },
      });
      return result(error, "Holder added.");
    }
    case "remove_secret_holder": {
      const { error } = await apiClient.DELETE("/secrets/{secret_id}/holders/{holder_id}", {
        params: { path: { secret_id: s(args.secret_id), holder_id: s(args.holder_id) } },
      });
      return result(error, "Holder removed.");
    }
    case "create_entity_schema": {
      const { error } = await apiClient.POST("/entities/schemas", {
        body: {
          key: s(args.key),
          workspace_id: workspaceId,
          definition: (args.definition ?? {}) as Record<string, never>,
        },
      });
      return result(error, "Schema version saved.");
    }
    case "transition_entity": {
      const { error } = await apiClient.POST("/entities/{entity_id}/transition", {
        params: { path: { entity_id: s(args.entity_id) } },
        body: {
          workspace_id: workspaceId,
          trigger: s(args.trigger),
          machine_key: args.machine_key ? s(args.machine_key) : "lifecycle",
          expected_version: args.expected_version ? num(args.expected_version) : null,
        },
      });
      return result(error, "Transition applied.");
    }
    case "update_workspace_settings": {
      const body: Record<string, unknown> = {};
      if (args.secret_mode) body.secret_mode = s(args.secret_mode);
      if (args.conduct_rules) body.conduct_rules = s(args.conduct_rules);
      if (args.allow_automerge !== undefined) body.allow_automerge = bool(args.allow_automerge);
      if (bool(args.clear_max_review_rounds)) body.max_review_rounds = null;
      else if (args.max_review_rounds) body.max_review_rounds = num(args.max_review_rounds);
      if (bool(args.clear_moderation_model)) body.moderation_model = null;
      else if (args.moderation_model) body.moderation_model = s(args.moderation_model);
      if (bool(args.clear_assistant_context_max_tokens)) body.assistant_context_max_tokens = null;
      else if (args.assistant_context_max_tokens)
        body.assistant_context_max_tokens = num(args.assistant_context_max_tokens);
      const { error } = await apiClient.PATCH("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
        body: body as never,
      });
      return result(error, "Settings updated.");
    }
    case "add_workspace_member": {
      const { error } = await apiClient.POST("/workspaces/{workspace_id}/members", {
        params: { path: { workspace_id: workspaceId } },
        body: { email: s(args.email), role: args.role ? s(args.role) : "participant" },
      });
      return result(error, "Member added.");
    }
    case "remove_workspace_member": {
      const { error } = await apiClient.DELETE(
        "/workspaces/{workspace_id}/members/{principal_id}",
        { params: { path: { workspace_id: workspaceId, principal_id: s(args.principal_id) } } },
      );
      return result(error, "Member removed.");
    }
    case "set_persona_scopes": {
      const { error } = await apiClient.PUT(
        "/workspaces/{workspace_id}/personas/{persona_id}/scopes",
        {
          params: { path: { workspace_id: workspaceId, persona_id: s(args.persona_id) } },
          body: { scopes: list(args.scopes) },
        },
      );
      return result(error, "Scopes set.");
    }
    case "advance_clock": {
      const { error } = await apiClient.POST("/workspaces/{workspace_id}/clock", {
        params: { path: { workspace_id: workspaceId } },
        body: { to_value: num(args.to_value) },
      });
      return result(error, "Clock advanced.");
    }
    case "create_workspace": {
      const { error } = await apiClient.POST("/workspaces", {
        body: { name: s(args.name), key: args.key ? s(args.key) : null },
      });
      return result(error, "Workspace created.");
    }
    case "archive_workspace": {
      const { error } = await apiClient.DELETE("/workspaces/{workspace_id}", {
        params: { path: { workspace_id: s(args.workspace_id) } },
      });
      return result(error, "Workspace archived.");
    }
    case "delegate_work": {
      const { error } = await apiClient.POST("/sessions/{session_id}/delegate", {
        params: { path: { session_id: s(args.session_id) } },
        body: {
          work_item_ids: list(args.work_item_ids),
          repo_id: args.repo_id ? s(args.repo_id) : null,
          server_key: "git",
          auto_review: args.auto_review === undefined ? true : bool(args.auto_review),
        },
      });
      return result(error, "Delegated.");
    }
    case "request_review_changes": {
      const { error } = await apiClient.POST("/sessions/{session_id}/review", {
        params: { path: { session_id: s(args.session_id) } },
        body: {
          work_item_id: s(args.work_item_id),
          branch: s(args.branch),
          comment: args.comment ? s(args.comment) : "Please address review feedback.",
          repo_id: args.repo_id ? s(args.repo_id) : null,
          server_key: "git",
        },
      });
      return result(error, "Changes requested.");
    }
    case "pause_session": {
      const { error } = await apiClient.POST("/sessions/{session_id}/pause", {
        params: { path: { session_id: s(args.session_id) } },
      });
      return result(error, "Session paused.");
    }
    case "resume_session": {
      const { error } = await apiClient.POST("/sessions/{session_id}/resume", {
        params: { path: { session_id: s(args.session_id) } },
      });
      return result(error, "Session resumed.");
    }
    case "wrap_up_session": {
      const { error } = await apiClient.POST("/sessions/{session_id}/conduct/wrap-up", {
        params: { path: { session_id: s(args.session_id) } },
      });
      return result(error, "Wrap-up requested.");
    }
    case "direct_turn": {
      const { error } = await apiClient.POST("/sessions/{session_id}/turns/generate", {
        params: { path: { session_id: s(args.session_id) } },
        body: { persona_id: s(args.persona_id) },
      });
      return result(error, "Turn requested.");
    }
    case "continue_session": {
      const { error } = await apiClient.POST("/sessions/{session_id}/continue", {
        params: { path: { session_id: s(args.session_id) } },
        body: { rounds: Math.min(10, Math.max(1, num(args.rounds) || 1)) },
      });
      return result(error, "Session continued.");
    }
    case "generate_report": {
      const { error } = await apiClient.POST("/reports", {
        body: { session_id: s(args.session_id), template_key: s(args.template_key) },
      });
      return result(error, "Report requested.");
    }
    case "request_recap": {
      const { error } = await apiClient.POST("/sessions/{session_id}/recap", {
        params: { path: { session_id: s(args.session_id) } },
      });
      return result(error, "Recap requested.");
    }
    case "archive_session": {
      const { error } = await apiClient.DELETE("/sessions/{session_id}", {
        params: { path: { session_id: s(args.session_id) } },
      });
      return result(error, "Session archived.");
    }
    case "archive_knowledge_source": {
      const id = await sourceIdByKey(s(args.source_key));
      if (!id) return { ok: false, detail: `no knowledge source ${s(args.source_key)}` };
      const { error } = await apiClient.DELETE("/knowledge/sources/{source_id}", {
        params: { path: { source_id: id } },
      });
      return result(error, "Source archived.");
    }
    case "set_workspace_vocabulary": {
      const key = s(args.overlay_key);
      const id = key ? await overlayIdByKey(key) : null;
      if (key && !id) return { ok: false, detail: `no vocabulary overlay ${key}` };
      const { error } = await apiClient.PATCH("/workspaces/{workspace_id}/vocabulary-overlay", {
        params: { path: { workspace_id: workspaceId } },
        body: { overlay_id: id },
      });
      return result(error, "Vocabulary set.");
    }
    case "set_tenant_default_vocabulary": {
      const { error } = await apiClient.PATCH("/tenant/vocabulary-overlay", {
        body: { overlay_key: args.overlay_key ? s(args.overlay_key) : null },
      });
      return result(error, "Default vocabulary set.");
    }
    case "register_repo": {
      // No token field exists on this tool, by design; the user adds it under Repos.
      const { error } = await apiClient.POST("/repos", {
        body: {
          key: s(args.key),
          name: s(args.name),
          description: s(args.description ?? ""),
          source_url: args.source_url ? s(args.source_url) : null,
          provider: args.provider ? s(args.provider) : null,
          runtime: args.runtime ? s(args.runtime) : "debian",
          runtime_image: args.runtime_image ? s(args.runtime_image) : null,
          setup_cmds: list(args.setup_cmds),
          test_cmd: args.test_cmd ? s(args.test_cmd) : null,
          build_cmd: args.build_cmd ? s(args.build_cmd) : null,
          artifact_name: args.artifact_name ? s(args.artifact_name) : null,
          preview_cmd: args.preview_cmd ? s(args.preview_cmd) : null,
          preview_port: args.preview_port ? num(args.preview_port) : null,
          preview_env: {},
        },
      });
      return result(error, "Repository registered. Add its access token under Repos.");
    }
    case "put_runtime": {
      const { error } = await apiClient.PUT("/repos/runtimes/{key}", {
        params: { path: { key: s(args.key) } },
        body: { image: s(args.image), setup: list(args.setup) },
      });
      return result(error, "Runtime saved.");
    }
    case "delete_runtime": {
      const { error } = await apiClient.DELETE("/repos/runtimes/{key}", {
        params: { path: { key: s(args.key) } },
      });
      return result(error, "Runtime deleted.");
    }
    case "refresh_repo": {
      const { error } = await apiClient.POST("/repos/{repo_id}/refresh", {
        params: { path: { repo_id: s(args.repo_id) } },
      });
      return result(error, "Repository refreshed.");
    }
    case "archive_repo": {
      const { error } = await apiClient.DELETE("/repos/{repo_id}", {
        params: { path: { repo_id: s(args.repo_id) } },
      });
      return result(error, "Repository archived.");
    }
    case "set_exec_engine": {
      const { error } = await apiClient.PUT("/repos/exec-engines/current", {
        body: { engine: s(args.engine) },
      });
      return result(error, "Engine set.");
    }
    case "upsert_mcp_server": {
      // credential_ref is deliberately absent: a credential is bound in the MCP page.
      const { error } = await apiClient.PUT("/mcp-servers", {
        body: {
          workspace_id: workspaceId,
          key: s(args.key),
          url: s(args.url),
          enabled_tools: list(args.enabled_tools),
          effectful_tools: list(args.effectful_tools),
          require_confirmation:
            args.require_confirmation === undefined ? true : bool(args.require_confirmation),
          max_calls_per_session: args.max_calls_per_session ? num(args.max_calls_per_session) : null,
          timeout_seconds: args.timeout_seconds ? num(args.timeout_seconds) : null,
          max_result_chars: args.max_result_chars ? num(args.max_result_chars) : null,
          options: {},
        },
      });
      return result(error, "MCP server saved.");
    }
    case "delete_mcp_server": {
      const { error } = await apiClient.DELETE("/mcp-servers/{key}", {
        params: { path: { key: s(args.key) }, query: { workspace_id: workspaceId } },
      });
      return result(error, "MCP server removed.");
    }
    case "set_limits": {
      const { error } = await apiClient.PUT("/limits", {
        body: {
          tenant_daily_tokens: num(args.tenant_daily_tokens),
          per_connection_daily_tokens: num(args.per_connection_daily_tokens),
          per_persona_daily_tokens: num(args.per_persona_daily_tokens),
          per_user_daily_tokens: num(args.per_user_daily_tokens),
        },
      });
      return result(error, "Limits saved.");
    }
    case "set_tenant_settings": {
      const body: Record<string, unknown> = {};
      if (args.session_lifetime_seconds) body.session_lifetime_seconds = num(args.session_lifetime_seconds);
      if (args.preview_ttl_seconds) body.preview_ttl_seconds = num(args.preview_ttl_seconds);
      if (args.reranker_enabled !== undefined) body.reranker_enabled = bool(args.reranker_enabled);
      const { error } = await apiClient.PUT("/tenant/settings", { body: body as never });
      return result(error, "Preferences saved.");
    }
    case "deploy_preview": {
      const { error } = await apiClient.POST("/previews", {
        body: {
          repo_id: s(args.repo_id),
          workspace_id: workspaceId,
          session_id: args.session_id ? s(args.session_id) : null,
          ttl_seconds: args.ttl_seconds ? num(args.ttl_seconds) : null,
          git_ref: args.git_ref ? s(args.git_ref) : "",
        },
      });
      return result(error, "Preview starting.");
    }
    case "stop_preview": {
      const { error } = await apiClient.DELETE("/previews/{preview_id}", {
        params: { path: { preview_id: s(args.preview_id) } },
      });
      return result(error, "Preview stopped.");
    }
    case "request_export": {
      // Never a password and never the connections section: both belong to the Export
      // page, where the person types the password. "full" mode needs one too.
      const mode = ["participant", "sanitised"].includes(s(args.mode)) ? s(args.mode) : "participant";
      const sections = list(args.sections).filter((x) => x !== "connections");
      const { error } = await apiClient.POST("/export", {
        body: { workspace_id: workspaceId, mode, sections: sections.length ? sections : null },
      });
      return result(error, "Export queued.");
    }
    default:
      return { ok: false, detail: `unknown action ${action}` };
  }
}
