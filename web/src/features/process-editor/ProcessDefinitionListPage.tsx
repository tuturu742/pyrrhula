import { useState } from "react";
import { toast } from "sonner";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";

/**
 * process-definition hub -- pick an existing definition to keep editing (its latest
 * version becomes the working draft), or start a fresh one from the template gallery.
 * "Never from an empty canvas" ( discipline 2): there is no "blank" option here at
 * all, only the two shipped fixtures.
 */
export function ProcessDefinitionListPage() {
  const t = useLabel();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [workspaceId, setWorkspaceId] = useState<string>("");

  const archive = useMutation({
    onError: () => toast.error("That didn't save — please try again."),
    mutationFn: async (definitionId: string) => {
      const { error } = await apiClient.DELETE("/process-definitions/{definition_id}", {
        params: { path: { definition_id: definitionId } },
      });
      if (error) throw error;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["process-definitions", workspaceId] }),
  });

  const { data: workspaces } = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces");
      if (error) throw error;
      return data;
    },
  });

  const { data: definitions, isLoading } = useQuery({
    queryKey: ["process-definitions", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions", {
        params: { query: workspaceId ? { workspace_id: workspaceId } : {} },
      });
      if (error) throw error;
      return data;
    },
  });

  const { data: templates } = useQuery({
    queryKey: ["process-definition-templates"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions/templates");
      if (error) throw error;
      return data;
    },
  });

  const latestByKey = new Map<string, NonNullable<typeof definitions>[number]>();
  for (const d of definitions ?? []) {
    const existing = latestByKey.get(d.key);
    if (!existing || existing.version < d.version) latestByKey.set(d.key, d);
  }

  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Process Definitions</h1>
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          {t("entity.workspace")}
          <select
            className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
          >
            <option value="">(tenant templates -- no workspace)</option>
            {workspaces?.map((ws) => (
              <option key={ws.id} value={ws.id}>
                {ws.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Existing definitions</h2>
        {isLoading && <p className="text-muted-foreground">Loading…</p>}
        {latestByKey.size === 0 && !isLoading && (
          <p className="text-muted-foreground">None yet in this scope -- start from a template below.</p>
        )}
        <ul className="flex flex-col gap-2">
          {[...latestByKey.values()].map((d) => (
            <li key={d.key} className="flex items-center gap-2">
              <Link
                to={`/process-definitions/${d.id}`}
                className="flex flex-1 items-center justify-between rounded-md border border-border px-4 py-3 hover:bg-accent"
              >
                <div>
                  <div className="font-medium">{d.name}</div>
                  <div className="text-sm text-muted-foreground">{d.key}</div>
                </div>
                <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">
                  v{d.version}
                </span>
              </Link>
              <ConfirmButton
                title="Archive"
                description={`Archive "${d.name}" (v${d.version})? It leaves this list and the launch picker; running sessions are unaffected. Reversible, not a permanent delete.`}
                confirmLabel="Archive"
                destructive
                onConfirm={() => archive.mutate(d.id)}
              >
                <button
                type="button"
                disabled={archive.isPending}
                className="shrink-0 rounded-md border border-destructive/50 px-3 py-1 text-sm text-destructive disabled:opacity-50"
              >
                Archive
              </button>
              </ConfirmButton>
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Start from a template</h2>
        <div className="grid grid-cols-2 gap-3">
          {templates?.map((tpl) => (
            <button
              key={tpl.key}
              type="button"
              onClick={() =>
                navigate(
                  `/process-definitions/new?template=${encodeURIComponent(tpl.key)}${
                    workspaceId ? `&workspace=${encodeURIComponent(workspaceId)}` : ""
                  }`,
                )
              }
              className="flex flex-col items-start gap-1 rounded-md border border-border p-4 text-left hover:bg-accent"
            >
              <span className="font-medium">{tpl.name}</span>
              <span className="text-xs text-muted-foreground">
                {Object.keys((tpl.definition as { phases?: Record<string, unknown> }).phases ?? {}).length}{" "}
                phases
              </span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
