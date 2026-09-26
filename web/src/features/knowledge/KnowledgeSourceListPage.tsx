import { useState } from "react";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";

/**
 * source list, tenant-wide (a knowledge source isn't workspace-scoped — it's
 * attached to zero or more workspaces, each with its own scope/priority/pin; see
 * KnowledgeSourceDetailPage's attachments panel). Library sources show up
 * here transparently via the backend's RLS disjunct, badged read-only.
 */
export function KnowledgeSourceListPage() {
  const t = useLabel();
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [sourceClass, setSourceClass] = useState("rules");

  const { data: sources, isLoading, error } = useQuery({
    queryKey: ["knowledge-sources"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/sources");
      if (error) throw error;
      return data;
    },
  });

  const createSource = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/knowledge/sources", {
        body: { key, name, class: sourceClass, visibility: "tenant" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setKey("");
      setName("");
      setShowCreateForm(false);
      void queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] });
    },
  });

  const archive = useMutation({
    mutationFn: async (sourceId: string) => {
      const { error } = await apiClient.DELETE("/knowledge/sources/{source_id}", {
        params: { path: { source_id: sourceId } },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] }),
  });

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!key.trim() || !name.trim()) return;
    createSource.mutate();
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{t("entity.knowledge_source")}s</h1>
        <button
          type="button"
          onClick={() => setShowCreateForm((v) => !v)}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
        >
          {showCreateForm ? "Cancel" : "New Source"}
        </button>
      </div>

      {showCreateForm && (
        <form
          onSubmit={handleCreate}
          className="flex flex-col gap-3 rounded-md border border-border p-4"
        >
          <div className="flex flex-col gap-1">
            <label className="text-sm text-muted-foreground" htmlFor="source-key">
              Key (stable identifier, e.g. "core-rules")
            </label>
            <input
              id="source-key"
              className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              required
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-sm text-muted-foreground" htmlFor="source-name">
              Name
            </label>
            <input
              id="source-name"
              className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-sm text-muted-foreground" htmlFor="source-class">
              Class
            </label>
            <select
              id="source-class"
              className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={sourceClass}
              onChange={(e) => setSourceClass(e.target.value)}
            >
              <option value="rules">{t("class.rules")}</option>
              <option value="lore">{t("class.lore")}</option>
              <option value="misc">{t("class.misc")}</option>
            </select>
          </div>
          {createSource.error !== null && (
            <p className="text-sm text-destructive">Failed to create source.</p>
          )}
          <button
            type="submit"
            disabled={createSource.isPending}
            className="self-start rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
          >
            Create
          </button>
        </form>
      )}

      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {error !== null && <p className="text-destructive">Failed to load sources.</p>}
      {sources?.length === 0 && (
        <p className="text-muted-foreground">
          No {t("entity.knowledge_source")}s yet — create one above.
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {sources?.map((source) => (
          <li key={source.id} className="flex items-center gap-2">
            <Link
              to={`/knowledge/${source.id}`}
              className="flex flex-1 items-center justify-between rounded-md border border-border px-4 py-3 hover:bg-accent"
            >
              <div>
                <div className="flex items-center gap-2 font-medium">
                  {source.name}
                  {source.is_library && (
                    <span className="rounded-full bg-secondary px-2 py-0.5 text-xs font-normal text-secondary-foreground">
                      library — read only
                    </span>
                  )}
                </div>
                <div className="text-sm text-muted-foreground">{source.key}</div>
              </div>
              <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">
                {t(`class.${source.class}`)}
              </span>
            </Link>
            {!source.is_library && (
              <ConfirmButton
                title="Archive"
                description={`Archive "${source.name}"? It detaches from all workspaces and leaves this list; its version history is kept. Reversible, not a permanent delete.`}
                confirmLabel="Archive"
                destructive
                onConfirm={() => archive.mutate(source.id)}
              >
                <button
                type="button"
                disabled={archive.isPending}
                className="shrink-0 rounded-md border border-destructive/50 px-3 py-1 text-sm text-destructive disabled:opacity-50"
              >
                Archive
              </button>
              </ConfirmButton>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
