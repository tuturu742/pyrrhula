import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useVocabularyStore } from "@/stores/vocabulary";

/**
 * resolves and activates the overlay for `workspaceId` (the fallback chain --
 * workspace override -> tenant default -> system default -- is computed server-side,
 * see `core.vocabulary.service.resolve_overlay_for_workspace`) and pushes it into
 * `useVocabularyStore`, which every `useLabel()` call subscribes to. Call this once
 * near the top of any page that has a workspace in scope; pass `null` (or omit) for
 * pages with no workspace context, which then show the tenant's own vocabulary (see
 * `useTenantVocabulary`). The workspace layer is cleared when the page unmounts.
 */
export function useWorkspaceVocabulary(workspaceId: string | null | undefined) {
  const setWorkspaceOverlay = useVocabularyStore((s) => s.setWorkspaceOverlay);
  const clearWorkspaceOverlay = useVocabularyStore((s) => s.clearWorkspaceOverlay);

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
    if (data) setWorkspaceOverlay(data.key, data.labels);
  }, [data, setWorkspaceOverlay]);

  useEffect(() => {
    if (!workspaceId) return;
    return () => clearWorkspaceOverlay();
  }, [workspaceId, clearWorkspaceOverlay]);
}

/**
 * the tenant's default overlay, for the shell itself and every page without a
 * workspace in scope. Loaded once per shell mount; a workspace page layered on top of
 * it wins while it is mounted.
 */
export function useTenantVocabulary(enabled = true) {
  const setTenantOverlay = useVocabularyStore((s) => s.setTenantOverlay);

  const { data } = useQuery({
    queryKey: ["tenant-vocabulary-overlay"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tenant/vocabulary-overlay");
      if (error) throw error;
      return data;
    },
    enabled,
  });

  useEffect(() => {
    if (data) setTenantOverlay(data.key, data.labels);
  }, [data, setTenantOverlay]);
}
