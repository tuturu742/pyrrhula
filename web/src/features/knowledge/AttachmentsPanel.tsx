import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import type { components } from "@/lib/api-client/schema";

type VersionResponse = components["schemas"]["VersionResponse"];

interface AttachmentsPanelProps {
  sourceId: string;
  versions: VersionResponse[] | undefined;
}

/**
 * D1.1: attach this source to a workspace with its own scope/priority/pin-vs-follow --
 * the acceptance criterion's "attach to a second workspace with a different priority"
 * without ever leaving this page.
 */
export function AttachmentsPanel({ sourceId, versions }: AttachmentsPanelProps) {
  const queryClient = useQueryClient();
  const [workspaceId, setWorkspaceId] = useState("");
  const [scopeKey, setScopeKey] = useState("workspace_public");
  const [priorityWeight, setPriorityWeight] = useState("1.0");
  const [versionPin, setVersionPin] = useState(""); // "" = follow latest
  const [enabled, setEnabled] = useState(true);

  const { data: workspaces } = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces");
      if (error) throw error;
      return data;
    },
  });

  const { data: attachments, isLoading } = useQuery({
    queryKey: ["knowledge-source-attachments", sourceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/sources/{source_id}/attachments", {
        params: { path: { source_id: sourceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const attach = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/knowledge/sources/{source_id}/attachments", {
        params: { path: { source_id: sourceId } },
        body: {
          workspace_id: workspaceId,
          scope_key: scopeKey,
          priority_weight: Number(priorityWeight),
          version_pin: versionPin || null,
          enabled,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge-source-attachments", sourceId] });
    },
  });

  function workspaceName(id: string): string {
    return workspaces?.find((w) => w.id === id)?.name ?? id;
  }

  return (
    <div className="flex flex-col gap-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!workspaceId) return;
          attach.mutate();
        }}
        className="grid grid-cols-2 gap-3 rounded-md border border-border p-4"
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Workspace</span>
          <select
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
            required
          >
            <option value="">select…</option>
            {workspaces?.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Scope</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={scopeKey}
            onChange={(e) => setScopeKey(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Priority weight</span>
          <input
            type="number"
            step="0.05"
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={priorityWeight}
            onChange={(e) => setPriorityWeight(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Version</span>
          <select
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={versionPin}
            onChange={(e) => setVersionPin(e.target.value)}
          >
            <option value="">Follow latest published</option>
            {versions?.map((v) => (
              <option key={v.id} value={v.id}>
                Pin to v{v.version_number}
              </option>
            ))}
          </select>
        </label>
        <label className="col-span-2 flex items-center gap-2 text-sm text-muted-foreground">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enabled
        </label>
        {attach.error !== null && (
          <p className="col-span-2 text-sm text-destructive">Failed to attach.</p>
        )}
        <button
          type="submit"
          disabled={attach.isPending}
          className="col-span-2 self-start rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          Attach to workspace
        </button>
      </form>

      {isLoading && <p className="text-muted-foreground">Loading attachments…</p>}
      {attachments?.length === 0 && (
        <p className="text-muted-foreground">Not attached to any workspace yet.</p>
      )}
      <ul className="flex flex-col gap-2">
        {attachments?.map((a) => (
          <li
            key={a.id}
            className="flex items-center justify-between rounded-md border border-border px-4 py-2 text-sm"
          >
            <div>
              <div className="font-medium">{workspaceName(a.workspace_id)}</div>
              <div className="text-xs text-muted-foreground">
                scope: {a.scope_key} · priority: {a.priority_weight} ·{" "}
                {a.version_pin ? "pinned" : "follows latest"} ·{" "}
                {a.enabled ? "enabled" : "disabled"}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
