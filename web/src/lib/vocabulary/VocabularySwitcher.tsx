import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useVocabularyStore } from "@/stores/vocabulary";

interface VocabularySwitcherProps {
  workspaceId: string;
}

/**
 * D1.6: per-workspace overlay switcher. Switching relabels the entire UI live -- on a
 * successful PATCH, the response's own `{key, labels}` is pushed straight into
 * `useVocabularyStore`, so every `useLabel()` consumer re-renders immediately, no reload
 * and no waiting on the invalidated query to refetch.
 */
export function VocabularySwitcher({ workspaceId }: VocabularySwitcherProps) {
  const queryClient = useQueryClient();
  const setOverlay = useVocabularyStore((s) => s.setOverlay);
  const activeOverlayKey = useVocabularyStore((s) => s.overlayKey);

  const { data: overlays } = useQuery({
    queryKey: ["vocabulary-overlays"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/vocabulary-overlays");
      if (error) throw error;
      return data;
    },
  });

  const switchOverlay = useMutation({
    mutationFn: async (overlayId: string) => {
      const { data, error } = await apiClient.PATCH("/workspaces/{workspace_id}/vocabulary-overlay", {
        params: { path: { workspace_id: workspaceId } },
        body: { overlay_id: overlayId },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (resolved) => {
      if (!resolved) return;
      setOverlay(resolved.key, resolved.labels);
      void queryClient.invalidateQueries({
        queryKey: ["workspace-vocabulary-overlay", workspaceId],
      });
    },
  });

  return (
    <label className="flex items-center gap-2 text-sm text-muted-foreground">
      Vocabulary
      <select
        className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        value={overlays?.find((o) => o.key === activeOverlayKey)?.id ?? ""}
        onChange={(e) => {
          if (e.target.value) switchOverlay.mutate(e.target.value);
        }}
        disabled={switchOverlay.isPending}
      >
        {overlays?.map((o) => (
          <option key={o.id} value={o.id}>
            {o.name}
          </option>
        ))}
      </select>
    </label>
  );
}
