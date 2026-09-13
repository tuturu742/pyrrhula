import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useVocabularyStore } from "@/stores/vocabulary";

/**
 * D1.6: resolves and activates the overlay for `workspaceId` (the fallback chain --
 * workspace override -> tenant default -> system default -- is computed server-side,
 * see `core.vocabulary.service.resolve_overlay_for_workspace`) and pushes it into
 * `useVocabularyStore`, which every `useLabel()` call subscribes to. Call this once
 * near the top of any page that has a workspace in scope; pass `null` (or omit) for
 * pages with no workspace context, which just keep the store's own default.
 */
export function useWorkspaceVocabulary(workspaceId: string | null | undefined) {
  const setOverlay = useVocabularyStore((s) => s.setOverlay);

  const { data } = useQuery({
    queryKey: ["workspace-vocabulary-overlay", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/vocabulary-overlay", {
        params: { path: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!workspaceId,
  });

  useEffect(() => {
    if (data) setOverlay(data.key, data.labels);
  }, [data, setOverlay]);
}
