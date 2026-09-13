import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useAuthStore } from "@/stores/auth";

/** The signed-in account, including the platform_admin flag the shell keys its admin
 * navigation on. The server re-checks admin on every /admin call — this is UI signal
 * only, never authorization. */
export function useMe() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["me"],
    enabled: !!token,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/me");
      if (error) throw error;
      return data;
    },
  });
}
