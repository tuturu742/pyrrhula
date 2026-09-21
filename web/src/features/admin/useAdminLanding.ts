import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

/**
 * Where a platform admin should land.
 *
 * Normally the tenant list. But the installers no longer download a retrieval model —
 * choosing one is the operator's decision, and this is the moment they are here to make
 * it. A deployment with no model installed is working but cannot answer a semantic
 * query, and "Models" is both the explanation and the fix, so that is the first screen
 * rather than something to be discovered later from an empty search.
 *
 * Only while nothing is downloaded. Once a model is present this stops firing, so it is
 * a first-run landing and not a nag: an admin who deliberately runs without one reaches
 * their usual page as soon as they have downloaded anything.
 */
export function useAdminLanding() {
  const cache = useQuery({
    queryKey: ["admin-retrieval-cache"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/retrieval-models/cache");
      if (error) throw error;
      return data as { models: { model: string; present: boolean }[] };
    },
    // The landing decision is made once per session; re-asking on every focus would
    // move the page under an admin who is working.
    staleTime: Infinity,
    retry: false,
  });

  if (cache.isLoading) return { loading: true as const, path: null };
  // An unreadable cache status is not a reason to send someone somewhere unexpected.
  const models = cache.data?.models;
  const nothingDownloaded = Array.isArray(models) && models.length > 0
    && models.every((m) => !m.present);
  return { loading: false as const, path: nothingDownloaded ? "/admin/models" : "/admin/tenants" };
}
