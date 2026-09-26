import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

type Draft = {
  session_lifetime_hours: number;
  preview_ttl_hours: number;
  reranker_enabled: boolean;
};

const HOUR = 3600;

/**
 * Organization preferences that used to be deployment environment variables: how long
 * a login lasts, how long a preview serves by default, and whether retrieval uses the
 * deployment's reranker. Two organizations reasonably disagree on all three, which is
 * why they live here and not in `.env`.
 */
export function OrganizationSettingsCard() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft | null>(null);

  const { data } = useQuery({
    queryKey: ["tenant-settings"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tenant/settings");
      if (error) throw error;
      return data;
    },
  });

  useEffect(() => {
    if (data && draft === null) {
      setDraft({
        session_lifetime_hours: data.session_lifetime_seconds / HOUR,
        preview_ttl_hours: data.preview_ttl_seconds / HOUR,
        reranker_enabled: data.reranker_enabled,
      });
    }
  }, [data, draft]);

  const save = useMutation({
    mutationFn: async (d: Draft) => {
      const { data, error } = await apiClient.PUT("/tenant/settings", {
        body: {
          session_lifetime_seconds: Math.round(d.session_lifetime_hours * HOUR),
          preview_ttl_seconds: Math.round(d.preview_ttl_hours * HOUR),
          reranker_enabled: d.reranker_enabled,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tenant-settings"] }),
  });

  if (!data || draft === null) return null;

  const dirty =
    Math.round(draft.session_lifetime_hours * HOUR) !== data.session_lifetime_seconds ||
    Math.round(draft.preview_ttl_hours * HOUR) !== data.preview_ttl_seconds ||
    draft.reranker_enabled !== data.reranker_enabled;
  const b = data.bounds;

  return (
    <div className="rounded-md border border-border p-4">
      <h2 className="font-medium">Preferences</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Organization-wide defaults. Each takes effect on the next login, preview or
        retrieval; nothing here needs a restart.
      </p>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Login lifetime (hours)</span>
          <input
            type="number"
            min={b.session_lifetime_min / HOUR}
            max={b.session_lifetime_max / HOUR}
            step={1}
            value={draft.session_lifetime_hours}
            onChange={(e) =>
              setDraft({ ...draft, session_lifetime_hours: Number(e.target.value) || 0 })
            }
            className="rounded-md border border-border bg-background px-2 py-1"
          />
          <span className="text-xs text-muted-foreground">
            {b.session_lifetime_min / 60} minutes to {b.session_lifetime_max / HOUR / 24}{" "}
            days; default {b.session_lifetime_default / HOUR} hours.
          </span>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Preview lifetime (hours)</span>
          <input
            type="number"
            min={b.preview_ttl_min / HOUR}
            max={b.preview_ttl_max / HOUR}
            step={0.5}
            value={draft.preview_ttl_hours}
            onChange={(e) =>
              setDraft({ ...draft, preview_ttl_hours: Number(e.target.value) || 0 })
            }
            className="rounded-md border border-border bg-background px-2 py-1"
          />
          <span className="text-xs text-muted-foreground">
            Up to {b.preview_ttl_max / HOUR} hours on this deployment; default{" "}
            {b.preview_ttl_default / HOUR}.
          </span>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Rerank retrieved knowledge</span>
          <span className="flex items-center gap-2 py-1">
            <input
              type="checkbox"
              checked={draft.reranker_enabled}
              disabled={!b.reranker_available}
              onChange={(e) => setDraft({ ...draft, reranker_enabled: e.target.checked })}
            />
            <span>{draft.reranker_enabled ? "On" : "Off"}</span>
          </span>
          <span className="text-xs text-muted-foreground">
            {b.reranker_available
              ? "A second, slower pass that reorders results by relevance. Off keeps fused rank order."
              : "This deployment has no reranker loaded (Admin → Models), so the switch has no effect."}
          </span>
        </label>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate(draft)}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          Save preferences
        </button>
        {save.error !== null && (
          <span className="text-sm text-destructive">
            {String(
              (save.error as { detail?: string })?.detail ??
                "Could not save — only organization owners and admins can change these.",
            )}
          </span>
        )}
        {save.isSuccess && !dirty && (
          <span className="text-sm text-muted-foreground">Saved.</span>
        )}
      </div>
    </div>
  );
}
