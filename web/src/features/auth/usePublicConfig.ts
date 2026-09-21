import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

export type PublicConfig = {
  single_tenant: boolean;
  allow_signup: boolean;
  has_organization: boolean;
};

/**
 * The deployment's shape, fetched before anyone signs in.
 *
 * Single-tenant mode used to be only half a feature: the API stopped requiring an
 * organization name and the login form went on asking for one anyway, because nothing
 * told it. A promise that a solo user never thinks about organizations is not kept by
 * making the field optional — it is kept by not showing it.
 *
 * Falls back to the multi-tenant shape when this cannot be read. That is the safe
 * direction: showing an organization field a deployment does not need is a small
 * annoyance, whereas hiding one it *does* need leaves someone unable to sign in at all.
 */
export function usePublicConfig() {
  const query = useQuery({
    queryKey: ["public-config"],
    queryFn: async (): Promise<PublicConfig> => {
      const { data, error } = await apiClient.GET("/auth/config");
      if (error) throw error;
      return data as PublicConfig;
    },
    // The shape of a deployment does not change while somebody is typing a password.
    staleTime: Infinity,
    retry: false,
  });

  return {
    loading: query.isLoading,
    config: query.data ?? {
      single_tenant: false,
      allow_signup: true,
      has_organization: true,
    },
  };
}
