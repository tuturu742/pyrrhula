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

async function sourceIdByKey(key: string): Promise<string | null> {
  const { data } = await apiClient.GET("/knowledge/sources");
  return data?.find((src) => src.key === key)?.id ?? null;
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
            scope_key: "workspace_public",
            keys: [],
            secondary_keys: [],
            logic: "AND",
            use_regex: false,
            constant: false,
            position: "before_char",
            insertion_order: 0,
          },
        },
      );
      return result(error, "Entry saved (draft).");
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
    default:
      return { ok: false, detail: `unknown action ${action}` };
  }
}
