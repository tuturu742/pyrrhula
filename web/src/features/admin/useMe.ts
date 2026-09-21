import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useAuthStore } from "@/stores/auth";

/** The signed-in account.
 *
 * `platform_admin` says what you may administer and is what the shell keys its admin
 * navigation on; `admin_tenant` says whether you are signed into the reserved admin
 * organization, which is the only one with no workspaces of its own to land on. They
 * differ for a single-tenant owner, who is both an ordinary user and an admin.
 *
 * The server re-checks admin on every /admin call — this is UI signal only, never
 * authorization. */
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
