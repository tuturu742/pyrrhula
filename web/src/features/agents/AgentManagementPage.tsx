import { useState } from "react";
import { BackLink } from "@/components/BackLink";
import { toast } from "sonner";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { useWorkspaceVocabulary } from "@/lib/vocabulary/useWorkspaceVocabulary";
import { AgentEditor } from "./AgentEditor";
import { VisibilityMatrixTab } from "./VisibilityMatrixTab";
import { ModelProfilesPanel } from "./ModelProfilesPanel";
import type { components } from "@/lib/api-client/schema";

type AgentResponse = components["schemas"]["api__routes__agents__PersonaResponse"];

/**
 * create and configure agents for a workspace -- persona, model profile, role
 * type -- plus tenant-wide model-profile management (provider credentials never
 * redisplayed once entered).
 */
export function AgentManagementPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const t = useLabel();
  useWorkspaceVocabulary(workspaceId);
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<AgentResponse | "new" | null>(null);
  const [subtab, setSubtab] = useState<"personas" | "visibility">("personas");

  const setWebSearch = useMutation({
    onError: () => toast.error("That didn't save — please try again."),
    mutationFn: async ({ personaId, on }: { personaId: string; on: boolean }) => {
      const { error } = await apiClient.PATCH("/agents/{persona_id}", {
        params: { path: { persona_id: personaId } },
        body: { web_search: on },
      });
      if (error) throw error;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] }),
  });

  // Which coding harness a persona's delegated work runs through. The list is what the
  // *tenant* has -- an operator can withhold one, and a deployment may serve none at all,
  // in which case the control is absent rather than offering something that would be
  // refused.
  const setHarness = useMutation({
    onError: () => toast.error("That didn't save — please try again."),
    mutationFn: async ({ personaId, harness }: { personaId: string; harness: string }) => {
      const { error } = await apiClient.PATCH("/agents/{persona_id}", {
        params: { path: { persona_id: personaId } },
        body: { harness },
      });
      if (error) throw error;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] }),
  });

  const archive = useMutation({
    onError: () => toast.error("That didn't save — please try again."),
    mutationFn: async (agentId: string) => {
      const { error } = await apiClient.DELETE("/agents/{persona_id}", {
        params: { path: { persona_id: agentId } },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] }),
  });

  // Fetching the assistant ENSURES it exists (it is required); refresh the roster once
  // it lands so a first visit shows it without a reload.
  useQuery({
    queryKey: ["assistant", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/assistant", {
        params: { path: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      void queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] });
      return data;
    },
    enabled: !!workspaceId,
    staleTime: Infinity,
  });

  const { data: agents, isLoading } = useQuery({
    queryKey: ["agents", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/agents", {
        params: { query: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!workspaceId,
  });

  const { data: harnesses } = useQuery({
    queryKey: ["harnesses"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/harnesses");
      if (error) throw error;
      return data;
    },
  });

  const { data: modelProfiles } = useQuery({
    queryKey: ["model-profiles"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles");
      if (error) throw error;
      return data;
    },
  });

  // Imported personas may point at the 'missing-connection' placeholder (provider
  // 'none') until a real connection is assigned -- warn loudly.
  const profileById = new Map((modelProfiles ?? []).map((mp) => [mp.id, mp]));
  const connectionMissing = (connectionId: string) => {
    const profile = profileById.get(connectionId);
    return !profile || profile.provider === "none";
  };

  if (!workspaceId) return null;

  return (
    <div className="flex flex-col gap-8">
      <BackLink to={`/workspaces/${workspaceId}`} label="Back to the workspace" />
      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">Personas</h1>
          {editing === null && (
            <button
              type="button"
              onClick={() => setEditing("new")}
              disabled={!modelProfiles || modelProfiles.length === 0}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              New persona
            </button>
          )}
        </div>
        <div className="flex gap-1 border-b border-border">
          {(["personas", "visibility"] as const).map((tk) => (
            <button
              key={tk}
              type="button"
              onClick={() => setSubtab(tk)}
              className={
                "border-b-2 px-3 py-1.5 text-sm " +
                (subtab === tk
                  ? "border-primary font-medium"
                  : "border-transparent text-muted-foreground hover:text-foreground")
              }
            >
              {tk === "personas" ? "Personas" : "Visibility"}
            </button>
          ))}
        </div>
        {subtab === "visibility" && <VisibilityMatrixTab workspaceId={workspaceId} />}
        {subtab === "personas" && modelProfiles?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            Create a model profile below before creating a persona.
          </p>
        )}

        {subtab === "personas" && editing === "new" && (
          <AgentEditor
            workspaceId={workspaceId}
            modelProfiles={modelProfiles ?? []}
            onSaved={() => setEditing(null)}
            onCancel={() => setEditing(null)}
          />
        )}

        {subtab === "personas" && isLoading && <p className="text-muted-foreground">Loading…</p>}
        {subtab === "personas" && agents?.length === 0 && !isLoading && (
          <p className="text-muted-foreground">No personas yet.</p>
        )}
        {subtab === "personas" && (
        <ul className="flex flex-col gap-2">
          {agents?.map((agent) => (
            <li
              key={agent.id}
              className="flex flex-col gap-3 rounded-md border border-border px-4 py-3"
            >
              <div className="flex items-center justify-between">
              <div>
                <div className="flex items-center gap-2 font-medium">
                  {agent.name}
                  {modelProfiles && connectionMissing(agent.agent_id) && (
                    <span
                      className="rounded-full border border-amber-500/60 bg-amber-500/10 px-2 py-0.5 text-xs font-normal text-amber-600 dark:text-amber-400"
                      title="This persona has no working model connection (it likely arrived in an import without one). Edit the persona and assign a connection — it cannot take turns until then."
                    >
                      ⚠ connection missing
                    </span>
                  )}
                  {agent.key === "assistant" && (
                    <span
                      className="rounded-full bg-secondary px-2 py-0.5 text-xs font-normal text-muted-foreground"
                      title="Every workspace carries an assistant for internal tasks (drafting personas, knowledge entries, answering questions). It cannot be archived."
                    >
                      Assistant · required
                    </span>
                  )}
                </div>
                <div className="text-sm text-muted-foreground">
                  {agent.key} · {t(`role.${agent.persona_type}`)}
                  {agent.entity_id && (
                    <>
                      {" · "}
                      <Link
                        to={`/workspaces/${workspaceId}/entities/${agent.entity_id}`}
                        className="underline hover:text-foreground"
                      >
                        {t("tab.main")}
                      </Link>
                    </>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <label
                  className="flex items-center gap-1.5 text-xs text-muted-foreground"
                  title="Let this persona search the internet during its turns"
                >
                  <input
                    type="checkbox"
                    checked={agent.web_search ?? false}
                    disabled={setWebSearch.isPending}
                    onChange={(e) =>
                      setWebSearch.mutate({ personaId: agent.id, on: e.target.checked })
                    }
                  />
                  Web search
                </label>
                {(harnesses?.length ?? 0) > 0 && agent.key !== "assistant" && (
                  <label
                    className="flex items-center gap-1.5 text-xs text-muted-foreground"
                    title="Delegated coding work for this persona runs through this harness — an agent loop with a shell, inside its container. Off means the one-shot path: the model emits whole files once."
                  >
                    Harness
                    <select
                      className="rounded-md border border-border bg-background px-1.5 py-0.5 text-xs"
                      value={agent.harness ?? ""}
                      disabled={setHarness.isPending}
                      onChange={(e) =>
                        setHarness.mutate({ personaId: agent.id, harness: e.target.value })
                      }
                    >
                      <option value="">none</option>
                      {harnesses?.map((h) => (
                        <option key={h.key} value={h.key}>
                          {h.key}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <button
                  type="button"
                  onClick={() =>
                    setEditing(editing !== "new" && editing?.id === agent.id ? null : agent)
                  }
                  className="rounded-md border border-border px-3 py-1 text-sm"
                >
                  {editing !== "new" && editing?.id === agent.id ? "Close" : "Edit"}
                </button>
                {agent.key !== "assistant" && (
                <ConfirmButton
                title="Archive"
                description={`Archive persona "${agent.name}"? It leaves every roster and stops taking turns. This is reversible via the database; it is not a permanent delete.`}
                confirmLabel="Archive"
                destructive
                onConfirm={() => archive.mutate(agent.id)}
              >
                <button
                  type="button"
                  disabled={archive.isPending}
                  className="rounded-md border border-destructive/50 px-3 py-1 text-sm text-destructive disabled:opacity-50"
                >
                  Archive
                </button>
              </ConfirmButton>
                )}
              </div>
              </div>
              {editing !== "new" && editing?.id === agent.id && (
                <AgentEditor
                  workspaceId={workspaceId}
                  modelProfiles={modelProfiles ?? []}
                  existingAgent={editing}
                  onSaved={() => setEditing(null)}
                  onCancel={() => setEditing(null)}
                />
              )}
            </li>
          ))}
        </ul>
        )}
      </section>

      <ModelProfilesPanel />
    </div>
  );
}
