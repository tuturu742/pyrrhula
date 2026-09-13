import { useQuery } from "@tanstack/react-query";
import { checkAccess } from "./api";

/** Shared by the nav entry (hide it without `secret:inspect`) and the route layout
 * itself (deny the actual Outlet) -- one query, one cache entry, so both agree. */
export function useDirectorViewAccess(workspaceId: string | undefined) {
  return useQuery({
    queryKey: ["director-view-access", workspaceId],
    queryFn: () => checkAccess(workspaceId!),
    enabled: !!workspaceId,
  });
}
