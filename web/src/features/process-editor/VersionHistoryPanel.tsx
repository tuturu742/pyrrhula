import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import type { ProcessDefinitionDSL } from "./dsl";

interface VersionHistoryPanelProps {
  workspaceId: string | null;
  defKey: string;
  currentVersion: number | null;
  onLoadVersion: (definition: ProcessDefinitionDSL, version: number) => void;
}

/**
 * D1.2 subtask: "definitions are versioned; publishing from the editor creates a new
 * version; read-only view of prior versions." There's no dedicated list-versions-of-a-key
 * endpoint (B1.1's own acceptance criteria never needed one -- `list_definitions` returns
 * every version of every key in a workspace/tenant-template scope); this panel fetches
 * that and filters client-side by key, same trade-off D1.1 made before adding a real
 * versions endpoint for knowledge sources turned out to be worth it there. Here the list
 * is already small (one row per publish), so client-side filtering is enough -- no
 * backend change needed.
 */
export function VersionHistoryPanel({
  workspaceId,
  defKey,
  currentVersion,
  onLoadVersion,
}: VersionHistoryPanelProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["process-definition-versions", workspaceId, defKey],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions", {
        params: { query: workspaceId ? { workspace_id: workspaceId } : {} },
      });
      if (error) throw error;
      return data;
    },
    enabled: defKey.trim() !== "",
  });

  const versions = (data ?? [])
    .filter((d) => d.key === defKey)
    .sort((a, b) => b.version - a.version);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      <h3 className="text-sm font-medium">Version history</h3>
      {isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
      {versions.length === 0 && !isLoading && (
        <p className="text-xs text-muted-foreground">Not published yet.</p>
      )}
      <ul className="flex flex-col gap-1">
        {versions.map((v) => (
          <li key={v.id} className="flex items-center justify-between text-xs">
            <span>
              v{v.version}
              {v.version === currentVersion && " (current draft base)"} —{" "}
              {new Date(v.created_at).toLocaleString()}
            </span>
            <button
              type="button"
              onClick={() => onLoadVersion(v.definition as unknown as ProcessDefinitionDSL, v.version)}
              className="rounded-md border border-border px-2 py-0.5"
            >
              View / restore
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
