import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

interface VersionsPanelProps {
  sourceId: string;
}

/**
 * D1.1: version history (newest first) + publish-draft-with-change-note + an
 * entry-level diff view between any two versions (added/removed/changed, with text
 * diffs from A1.8's own diff endpoint — this component renders that response, it
 * doesn't recompute anything).
 */
export function VersionsPanel({ sourceId }: VersionsPanelProps) {
  const queryClient = useQueryClient();
  const [changeNote, setChangeNote] = useState("");
  const [fromVersionId, setFromVersionId] = useState("");
  const [toVersionId, setToVersionId] = useState("");

  const { data: versions, isLoading } = useQuery({
    queryKey: ["knowledge-versions", sourceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/sources/{source_id}/versions", {
        params: { path: { source_id: sourceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const publish = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/knowledge/sources/{source_id}/publish", {
        params: { path: { source_id: sourceId } },
        body: { change_note: changeNote || null },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setChangeNote("");
      void queryClient.invalidateQueries({ queryKey: ["knowledge-versions", sourceId] });
      void queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] });
    },
  });

  const diffQuery = useQuery({
    queryKey: ["knowledge-diff", sourceId, fromVersionId, toVersionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/sources/{source_id}/diff", {
        params: {
          path: { source_id: sourceId },
          query: { from_version_id: fromVersionId, to_version_id: toVersionId },
        },
      });
      if (error) throw error;
      return data;
    },
    enabled: fromVersionId !== "" && toVersionId !== "" && fromVersionId !== toVersionId,
  });

  return (
    <div className="flex flex-col gap-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          publish.mutate();
        }}
        className="flex items-end gap-2"
      >
        <label className="flex flex-1 flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Change note (optional)</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={changeNote}
            onChange={(e) => setChangeNote(e.target.value)}
            placeholder="What changed in this publish?"
          />
        </label>
        <button
          type="submit"
          disabled={publish.isPending}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          Publish draft
        </button>
      </form>
      {publish.error !== null && (
        <p className="text-sm text-destructive">Failed to publish (draft may be empty).</p>
      )}

      {isLoading && <p className="text-muted-foreground">Loading versions…</p>}
      {versions?.length === 0 && (
        <p className="text-muted-foreground">No published versions yet.</p>
      )}

      <ul className="flex flex-col gap-2">
        {versions?.map((v) => (
          <li
            key={v.id}
            className="flex items-center justify-between rounded-md border border-border px-4 py-2 text-sm"
          >
            <div>
              <span className="font-medium">v{v.version_number}</span>
              {v.change_note && (
                <span className="ml-2 text-muted-foreground">{v.change_note}</span>
              )}
            </div>
            <span className="font-mono text-xs text-muted-foreground">
              {v.content_hash.slice(0, 12)}
            </span>
          </li>
        ))}
      </ul>

      {versions && versions.length >= 2 && (
        <div className="flex flex-col gap-3 rounded-md border border-dashed border-border p-4">
          <div className="text-sm font-medium">Compare versions</div>
          <div className="flex gap-2">
            <select
              className="flex-1 rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={fromVersionId}
              onChange={(e) => setFromVersionId(e.target.value)}
            >
              <option value="">from…</option>
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version_number}
                </option>
              ))}
            </select>
            <select
              className="flex-1 rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={toVersionId}
              onChange={(e) => setToVersionId(e.target.value)}
            >
              <option value="">to…</option>
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version_number}
                </option>
              ))}
            </select>
          </div>

          {diffQuery.data && <DiffView diff={diffQuery.data} />}
        </div>
      )}
    </div>
  );
}

function DiffView({
  diff,
}: {
  diff: { added: string[]; removed: string[]; changed: { entry_key: string; text_diff: string }[] };
}) {
  if (diff.added.length === 0 && diff.removed.length === 0 && diff.changed.length === 0) {
    return <p className="text-sm text-muted-foreground">No differences.</p>;
  }
  return (
    <div className="flex flex-col gap-3 text-sm">
      {diff.added.length > 0 && (
        <div>
          <div className="font-medium text-green-600 dark:text-green-400">Added</div>
          <ul className="list-inside list-disc text-muted-foreground">
            {diff.added.map((key) => (
              <li key={key}>{key}</li>
            ))}
          </ul>
        </div>
      )}
      {diff.removed.length > 0 && (
        <div>
          <div className="font-medium text-destructive">Removed</div>
          <ul className="list-inside list-disc text-muted-foreground">
            {diff.removed.map((key) => (
              <li key={key}>{key}</li>
            ))}
          </ul>
        </div>
      )}
      {diff.changed.length > 0 && (
        <div className="flex flex-col gap-2">
          <div className="font-medium">Changed</div>
          {diff.changed.map((c) => (
            <div key={c.entry_key} className="rounded-md bg-secondary p-2">
              <div className="text-xs text-muted-foreground">{c.entry_key}</div>
              <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-xs">
                {c.text_diff}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
