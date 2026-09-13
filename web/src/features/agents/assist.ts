import { useMutation, useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

/** The sole workspace's id (single-workspace tenants are the norm; multi-workspace
 * tenants get the first). Used by editors that need a workspace-scoped assistant but
 * live on tenant-level pages (e.g. the knowledge entry editor). */
export function useSoleWorkspaceId(): string | undefined {
  const { data } = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces");
      if (error) throw error;
      return data;
    },
  });
  return data?.[0]?.id;
}

export interface AssistArgs {
  task: "draft_persona" | "draft_knowledge" | "ask";
  subject: string;
  instruction: string;
}

/** One assistant call. The server ensures the workspace's (required) assistant exists,
 * grounds the request in the caller's entitled workspace knowledge, and returns a draft
 * or answer. */
export function useAssist(workspaceId: string | undefined) {
  return useMutation({
    mutationFn: async (args: AssistArgs) => {
      if (!workspaceId) throw new Error("no workspace");
      const { data, error } = await apiClient.POST("/workspaces/{workspace_id}/assist", {
        params: { path: { workspace_id: workspaceId } },
        body: args,
      });
      if (error) throw error;
      return data;
    },
  });
}
