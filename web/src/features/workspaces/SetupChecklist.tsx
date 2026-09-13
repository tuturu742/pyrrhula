import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";

/**
 * First-run guide: a fresh workspace can't do anything until it has a model
 * connection and a persona roster. Shown only while incomplete; the starter-team
 * button gets a new user from "empty" to "launchable" in two clicks.
 */
export function SetupChecklist({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [starterConnection, setStarterConnection] = useState("");

  const { data: profiles } = useQuery({
    queryKey: ["model-profiles"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles");
      if (error) throw error;
      return data;
    },
  });
  const { data: personas } = useQuery({
    queryKey: ["agents", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/agents", {
        params: { query: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const starterTeam = useMutation({
    mutationFn: async (agentId: string) => {
      const { error } = await apiClient.POST("/agents/starter-team", {
        body: { workspace_id: workspaceId, agent_id: agentId },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] }),
  });

  if (profiles === undefined || personas === undefined) return null;
  const hasConnection = profiles.length > 0;
  // The auto-created assistant doesn't make a roster; count actable personas.
  const roster = personas.filter((p) => p.key !== "assistant");
  const hasRoster = roster.some((p) => p.persona_type === "supervisor") && roster.length >= 2;
  if (hasConnection && hasRoster) return null;

  const usable = profiles.filter((p) => p.provider !== "echo");

  return (
    <section className="flex flex-col gap-3 rounded-md border border-primary/40 bg-primary/5 p-4">
      <h2 className="font-medium">Get this workspace running</h2>
      <ol className="flex flex-col gap-2 text-sm">
        <li className="flex items-center gap-2">
          <span>{hasConnection ? "✅" : "1️⃣"}</span>
          <span>
            Add a <b>model connection</b> (a cloud API key or local Ollama) —{" "}
            <Link to="/personas" className="underline">
              Personas → Connections
            </Link>
          </span>
        </li>
        <li className="flex items-start gap-2">
          <span>{hasRoster ? "✅" : "2️⃣"}</span>
          <span className="flex flex-col gap-1.5">
            <span>
              Create a <b>persona roster</b> (one supervisor + participants).
            </span>
            {!hasRoster && hasConnection && (
              <span className="flex items-center gap-2">
                <select
                  className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={starterConnection}
                  onChange={(e) => setStarterConnection(e.target.value)}
                >
                  <option value="">(pick a connection)</option>
                  {usable.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ({p.provider}/{p.model})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={!starterConnection || starterTeam.isPending}
                  onClick={() => starterTeam.mutate(starterConnection)}
                  className="rounded-md bg-primary px-3 py-1 text-sm font-medium text-primary-foreground disabled:opacity-50"
                >
                  {starterTeam.isPending ? "Creating…" : "Create starter team"}
                </button>
              </span>
            )}
          </span>
        </li>
        <li className="flex items-center gap-2">
          <span>3️⃣</span>
          <span>
            Pick a flow below and <b>launch your first session</b> — the 💬 assistant
            (bottom right) can draft agendas, personas, and knowledge for you.
          </span>
        </li>
      </ol>
      {starterTeam.error !== null && (
        <p className="text-sm text-destructive">Starter team creation failed — try again.</p>
      )}
    </section>
  );
}
