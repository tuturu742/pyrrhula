/**
 * Preview deployments, shared by the Repos page and the session view.
 *
 * The two places need the same four operations and, more importantly, the same rules
 * about them: a share URL is minted on demand rather than stored, so the answer to "my
 * link stopped working" is to ask for another one -- and only a *running* preview can
 * give you one, because no token resurrects a container the reaper already removed.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "@/lib/api-client/client";

export interface PreviewRow {
  id: string;
  status: string;
  repo_id?: string | null;
  expires_at?: string | null;
  last_error?: string;
  artifact_name?: string;
}

/** Every preview the tenant has, finished ones included -- an expired row is exactly the
 *  case the user needs to see, since it is the one whose link just broke. */
export function usePreviews(options?: { repoId?: string; enabled?: boolean }) {
  return useQuery({
    queryKey: ["previews", options?.repoId ?? "all"],
    enabled: options?.enabled ?? true,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/previews", {
        params: {
          query: { include_finished: true, ...(options?.repoId ? { repo_id: options.repoId } : {}) },
        },
      });
      if (error) throw error;
      return (data ?? []) as PreviewRow[];
    },
    // A preview takes a little while to pull its image and unpack the build.
    refetchInterval: 6000,
  });
}

export function usePreviewActions(setNotice: (text: string) => void) {
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["previews"] });

  const deploy = useMutation({
    mutationFn: async (vars: { repoId: string; sessionId?: string; gitRef?: string }) => {
      const { data, error } = await apiClient.POST("/previews", {
        body: {
          repo_id: vars.repoId,
          ...(vars.sessionId ? { session_id: vars.sessionId } : {}),
          // Empty means the repository's default artifact slot; a branch means that
          // branch's own build, which is what makes two pull requests two previews
          // rather than one that keeps replacing itself.
          git_ref: vars.gitRef ?? "",
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setNotice("Deploying — the link appears once it is running.");
      refresh();
    },
    onError: () => setNotice("Could not start a preview. Is there a built artifact yet?"),
  });

  const share = useMutation({
    mutationFn: async (previewId: string) => {
      const { data, error } = await apiClient.POST("/previews/{preview_id}/share", {
        params: { path: { preview_id: previewId } },
        body: { extend: true },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: async (data) => {
      const url = absoluteUrl(data?.url ?? "");
      try {
        await navigator.clipboard.writeText(url);
        setNotice(`Link copied — it needs no login, and expires with the preview. ${url}`);
      } catch {
        // Clipboard needs a secure context and permission; show the link either way.
        setNotice(url);
      }
      refresh();
    },
    onError: () => setNotice("This preview is no longer running — deploy it again."),
  });

  const stop = useMutation({
    mutationFn: async (previewId: string) => {
      const { error } = await apiClient.DELETE("/previews/{preview_id}", {
        params: { path: { preview_id: previewId } },
      });
      if (error) throw error;
    },
    onSuccess: refresh,
  });

  return { deploy, share, stop };
}

/** Share URLs come back site-relative; a link meant to be pasted to someone else has to
 *  carry the host. */
export function absoluteUrl(url: string): string {
  if (!url) return "";
  try {
    return new URL(url, window.location.origin).toString();
  } catch {
    return url;
  }
}


/** The pull requests a repo's delegations opened, for picking which one to preview. */
export function useRepoPullRequests(repoId: string | undefined) {
  return useQuery({
    queryKey: ["repo-pull-requests", repoId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos/{repo_id}/pull-requests", {
        params: { path: { repo_id: repoId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: Boolean(repoId),
    // A delegation's build lands a little after its branch does.
    refetchInterval: 20000,
  });
}
