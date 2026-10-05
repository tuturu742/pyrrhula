import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client/client";
import { applyAssistantAction } from "../assistant-actions";

vi.mock("@/lib/api-client/client", () => ({
  apiClient: { GET: vi.fn(), POST: vi.fn(), PUT: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() },
}));

const ok = async () => ({ data: {}, error: undefined });
const verbs = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;

beforeEach(() => {
  for (const verb of verbs) vi.mocked(apiClient[verb]).mockReset();
  for (const verb of verbs) vi.mocked(apiClient[verb]).mockImplementation(ok as never);
  vi.mocked(apiClient.GET).mockImplementation((async (path: string) => {
    if (path === "/knowledge/sources") return { data: [{ id: "src-1", key: "rules" }] };
    if (path === "/vocabulary-overlays") return { data: [{ id: "ov-1", key: "swdev_v1" }] };
    return { data: [] };
  }) as never);
});

function bodyOf(verb: (typeof verbs)[number], call = 0): Record<string, unknown> {
  const args = vi.mocked(apiClient[verb]).mock.calls[call] as unknown[];
  return (args[1] as { body?: Record<string, unknown> })?.body ?? {};
}

describe("applyAssistantAction", () => {
  it("injects the workspace into create_secret and defaults publication", async () => {
    const res = await applyAssistantAction(
      "create_secret",
      { subject_kind: "entity", subject_id: "e1", content: "x", gist: "g", scope_key: "k" },
      "ws-1",
    );
    expect(res.ok).toBe(true);
    expect(bodyOf("POST")).toMatchObject({
      workspace_id: "ws-1",
      publication: "guarded",
      hint_text: null,
    });
  });

  it("update_secret omits untouched fields and sends null only to clear", async () => {
    await applyAssistantAction("update_secret", { secret_id: "s1", gist: "new" }, "ws-1");
    expect(bodyOf("PATCH")).toEqual({ gist: "new" });
    await applyAssistantAction("update_secret", { secret_id: "s1", clear_hint: "true" }, "ws-1");
    expect(bodyOf("PATCH", 1)).toEqual({ hint_text: null });
  });

  it("transition_entity defaults the machine and coerces the version", async () => {
    await applyAssistantAction(
      "transition_entity",
      { entity_id: "e1", trigger: "approve", expected_version: "3" },
      "ws-1",
    );
    expect(bodyOf("POST")).toEqual({
      workspace_id: "ws-1",
      trigger: "approve",
      machine_key: "lifecycle",
      expected_version: 3,
    });
  });

  it("reports an unknown action as a failed apply", async () => {
    const res = await applyAssistantAction("launch_rockets", {}, "ws-1");
    expect(res).toEqual({ ok: false, detail: "unknown action launch_rockets" });
  });

  it("never forwards credential-shaped fields, whatever the model put in args", async () => {
    const hostile = {
      access_token: "ghp_LEAKED",
      registry_token: "LEAKED2",
      credential_ref: "LEAKED3",
      password: "LEAKED4",
    };
    for (const action of ACTIONS) {
      await applyAssistantAction(action, { ...SAMPLE_ARGS, ...hostile }, "ws-1");
    }
    const everything = JSON.stringify(verbs.map((v) => vi.mocked(apiClient[v]).mock.calls));
    for (const leak of Object.values(hostile)) expect(everything).not.toContain(leak);
  });
});

describe("group 2 coercions", () => {
  it("delegate_work splits a comma list and defaults auto_review", async () => {
    await applyAssistantAction("delegate_work", { session_id: "s1", work_item_ids: "e1, e2" }, "ws-1");
    expect(bodyOf("POST")).toEqual({
      work_item_ids: ["e1", "e2"],
      repo_id: null,
      server_key: "git",
      auto_review: true,
    });
  });

  it("continue_session clamps rounds to 1..10", async () => {
    await applyAssistantAction("continue_session", { session_id: "s1", rounds: "40" }, "ws-1");
    expect(bodyOf("POST")).toEqual({ rounds: 10 });
  });

  it("update_workspace_settings sends only what was given, null only to clear", async () => {
    await applyAssistantAction(
      "update_workspace_settings",
      { secret_mode: "gate", clear_moderation_model: "true" },
      "ws-1",
    );
    expect(bodyOf("PATCH")).toEqual({ secret_mode: "gate", moderation_model: null });
  });

  it("set_workspace_vocabulary resolves a key to an id and empty to inherit", async () => {
    await applyAssistantAction("set_workspace_vocabulary", { overlay_key: "swdev_v1" }, "ws-1");
    expect(bodyOf("PATCH")).toEqual({ overlay_id: "ov-1" });
    await applyAssistantAction("set_workspace_vocabulary", { overlay_key: "" }, "ws-1");
    expect(bodyOf("PATCH", 1)).toEqual({ overlay_id: null });
  });
});

describe("group 3 guards", () => {
  it("request_export never sends a password, drops connections and downgrades full", async () => {
    await applyAssistantAction(
      "request_export",
      { mode: "full", sections: "personas,connections", password: "hunter2" },
      "ws-1",
    );
    expect(bodyOf("POST")).toEqual({ workspace_id: "ws-1", mode: "participant", sections: ["personas"] });
  });

  it("register_repo has no token field and defaults the runtime", async () => {
    await applyAssistantAction("register_repo", { key: "k", name: "n", access_token: "ghp_x" }, "ws-1");
    const body = bodyOf("POST");
    expect(body.runtime).toBe("debian");
    expect(Object.keys(body)).not.toContain("access_token");
  });

  it("delete_mcp_server passes the workspace as a query parameter", async () => {
    await applyAssistantAction("delete_mcp_server", { key: "evidence" }, "ws-1");
    const [, opts] = vi.mocked(apiClient.DELETE).mock.calls[0] as unknown[];
    expect((opts as { params: { query: unknown } }).params.query).toEqual({ workspace_id: "ws-1" });
  });
});

/** Every action the switch knows, kept in step with the backend by the parity test. */
const ACTIONS = [
  "update_persona",
  "create_persona",
  "archive_persona",
  "upsert_knowledge_entry",
  "attach_knowledge_source",
  "create_process_definition",
  "create_session",
  "create_knowledge_source",
  "publish_knowledge_source",
  "rename_session",
  "set_session_agenda",
  "set_turn_policy",
  "update_workflow",
  "set_current_workflow",
  "update_repo",
  "create_model_profile",
  "update_model_profile",
  "create_secret",
  "update_secret",
  "add_secret_holder",
  "remove_secret_holder",
  "create_entity_schema",
  "transition_entity",
  "update_workspace_settings",
  "add_workspace_member",
  "remove_workspace_member",
  "set_persona_scopes",
  "advance_clock",
  "create_workspace",
  "archive_workspace",
  "delegate_work",
  "request_review_changes",
  "pause_session",
  "resume_session",
  "wrap_up_session",
  "direct_turn",
  "continue_session",
  "generate_report",
  "request_recap",
  "archive_session",
  "archive_knowledge_source",
  "set_workspace_vocabulary",
  "set_tenant_default_vocabulary",
  "register_repo",
  "put_runtime",
  "delete_runtime",
  "refresh_repo",
  "archive_repo",
  "set_exec_engine",
  "upsert_mcp_server",
  "delete_mcp_server",
  "set_limits",
  "set_tenant_settings",
  "deploy_preview",
  "stop_preview",
  "request_export",
];

const SAMPLE_ARGS: Record<string, unknown> = {
  persona_id: "p1",
  key: "k",
  name: "n",
  agent_id: "a1",
  source_key: "rules",
  entry_key: "e",
  title: "t",
  body_md: "b",
  scope_key: "workspace_public",
  definition: {},
  process_definition_id: "pd1",
  supervisor_persona_id: "p1",
  session_id: "s1",
  turn_policy: "autonomous",
  workflow_key: "swdev",
  repo_id: "r1",
  provider: "openai",
  model: "m",
  profile_id: "pr1",
  secret_id: "s1",
  subject_kind: "entity",
  subject_id: "e1",
  content: "c",
  gist: "g",
  holder_principal_id: "pr1",
  holder_kind: "told",
  holder_id: "h1",
  entity_id: "e1",
  trigger: "approve",
  email: "x@example.com",
  principal_id: "pr1",
  scopes: "a,b",
  to_value: "3",
  workspace_id: "ws-2",
  work_item_ids: "e1,e2",
  work_item_id: "e1",
  branch: "feature",
  rounds: "2",
  template_key: "session_log",
  overlay_key: "swdev_v1",
  image: "img:1",
  engine: "podman",
  url: "http://mcp",
  enabled_tools: "a,b",
  preview_id: "pv1",
  mode: "full",
  sections: "personas,connections",
};
