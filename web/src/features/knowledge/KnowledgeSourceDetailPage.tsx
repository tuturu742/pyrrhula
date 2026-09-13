import { useState } from "react";
import { BackLink } from "@/components/BackLink";
import { toast } from "sonner";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { EntryEditor } from "./EntryEditor";
import { VersionsPanel } from "./VersionsPanel";
import { AttachmentsPanel } from "./AttachmentsPanel";
import { UploadPanel } from "./UploadPanel";
import type { components } from "@/lib/api-client/schema";

type EntryResponse = components["schemas"]["EntryResponse"];

/**
 * D1.1: the authoring hub for one Knowledge Source — draft entries (create/edit with
 * full activation fields), upload/ingestion, version history + publish + diff, and
 * workspace attachments, all without leaving this page (the acceptance criterion's
 * "full authoring loop without touching the API directly").
 */
export function KnowledgeSourceDetailPage() {
  const { sourceId } = useParams<{ sourceId: string }>();
  const navigate = useNavigate();
  const t = useLabel();
  const queryClient = useQueryClient();
  const [editingEntry, setEditingEntry] = useState<EntryResponse | "new" | null>(null);

  const { data: sources, isError: sourceLoadFailed, isSuccess: sourcesLoaded } = useQuery({
    queryKey: ["knowledge-sources"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/sources");
      if (error) throw error;
      return data;
    },
  });
  const source = sources?.find((s) => s.id === sourceId);

  const { data: entries, isLoading: entriesLoading } = useQuery({
    queryKey: ["knowledge-entries", sourceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/sources/{source_id}/entries", {
        params: { path: { source_id: sourceId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sourceId,
  });

  const { data: versions } = useQuery({
    queryKey: ["knowledge-versions", sourceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/sources/{source_id}/versions", {
        params: { path: { source_id: sourceId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sourceId,
  });

  const fork = useMutation({
    onError: () => toast.error("That didn't save — please try again."),
    mutationFn: async () => {
      if (!source?.current_version_id) {
        throw new Error("Source has no published version to fork from.");
      }
      const { data, error } = await apiClient.POST("/knowledge/sources/{source_id}/fork", {
        params: { path: { source_id: sourceId! } },
        body: {
          from_version_id: source.current_version_id,
          new_key: `${source.key}-fork-${Date.now().toString(36)}`,
          new_name: `${source.name} (fork)`,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (forked) => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] });
      if (forked) navigate(`/knowledge/${forked.id}`);
    },
  });

  if (!sourceId) return null;

  return (
    <div className="flex flex-col gap-8">
      <BackLink to={"/knowledge"} label="All sources" />
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-semibold">{source?.name ??
            (sourceLoadFailed || (sourcesLoaded && !source) ? "Source not found" : "Loading…")}</h1>
      {(sourceLoadFailed || (sourcesLoaded && !source)) && (
        <p className="text-sm text-destructive">
          This knowledge source could not be loaded — it may have been archived.
        </p>
      )}
            {source?.is_library && (
              <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                library — read only, editing creates your own copy
              </span>
            )}
            {source && (
              <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">
                {t(`class.${source.class}`)}
              </span>
            )}
          </div>
          <div className="text-sm text-muted-foreground">{source?.key}</div>
        </div>
        {source?.is_library && (
          <button
            type="button"
            onClick={() => fork.mutate()}
            disabled={fork.isPending}
            className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
          >
            Fork this source
          </button>
        )}
      </div>

      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">Entries</h2>
          {editingEntry === null && (
            <button
              type="button"
              onClick={() => setEditingEntry("new")}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
            >
              New entry
            </button>
          )}
        </div>

        {editingEntry !== null && (
          <EntryEditor
            sourceId={sourceId}
            existingEntry={editingEntry === "new" ? undefined : editingEntry}
            onSaved={(entry) => {
              setEditingEntry(null);
              if (entry.forked_source_id) navigate(`/knowledge/${entry.forked_source_id}`);
            }}
            onCancel={() => setEditingEntry(null)}
          />
        )}

        {entriesLoading && <p className="text-muted-foreground">Loading entries…</p>}
        {entries?.length === 0 && (
          <p className="text-muted-foreground">No draft entries yet — add one above.</p>
        )}
        <ul className="flex flex-col gap-2">
          {entries?.map((entry) => (
            <li
              key={entry.id}
              className="flex items-center justify-between rounded-md border border-border px-4 py-2"
            >
              <div>
                <div className="font-medium">{entry.title}</div>
                <div className="text-xs text-muted-foreground">
                  {entry.entry_key}
                  {entry.constant && " · constant"}
                  {entry.keys.length > 0 && ` · keys: ${entry.keys.join(", ")}`}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setEditingEntry(entry)}
                className="rounded-md border border-border px-3 py-1 text-sm"
              >
                Edit
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Upload &amp; ingest</h2>
        <UploadPanel sourceId={sourceId} />
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Versions</h2>
        <VersionsPanel sourceId={sourceId} />
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">{t("entity.workspace")} attachments</h2>
        <AttachmentsPanel sourceId={sourceId} versions={versions} />
      </section>
    </div>
  );
}
