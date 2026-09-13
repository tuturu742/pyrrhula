import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

const FIELDS = [
  { key: "tenant_daily_tokens", label: "Organization (all activity)" },
  { key: "per_connection_daily_tokens", label: "Per connection" },
  { key: "per_persona_daily_tokens", label: "Per persona" },
  { key: "per_user_daily_tokens", label: "Per user" },
] as const;

type LimitKey = (typeof FIELDS)[number]["key"];
type Limits = Record<LimitKey, number>;

export function UsageLimitsCard() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Limits | null>(null);

  const { data } = useQuery({
    queryKey: ["usage-limits"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/limits");
      if (error) throw error;
      return data;
    },
  });

  useEffect(() => {
    if (data && draft === null) {
      setDraft({
        tenant_daily_tokens: data.tenant_daily_tokens ?? 0,
        per_connection_daily_tokens: data.per_connection_daily_tokens ?? 0,
        per_persona_daily_tokens: data.per_persona_daily_tokens ?? 0,
        per_user_daily_tokens: data.per_user_daily_tokens ?? 0,
      });
    }
  }, [data, draft]);

  const save = useMutation({
    mutationFn: async (body: Limits) => {
      const { data, error } = await apiClient.PUT("/limits", { body });
      if (error) throw error;
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["usage-limits"] }),
  });

  if (!data || draft === null) return null;

  const dirty = FIELDS.some((f) => draft[f.key] !== (data[f.key] ?? 0));

  return (
    <div className="rounded-md border border-border p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="font-medium">Daily usage limits</h2>
        <span className="text-sm text-muted-foreground">
          used today: {(data.tenant_used_today ?? 0).toLocaleString()} tokens
        </span>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">
        Hard caps on model tokens per UTC day. 0 = unlimited. When a cap is hit, sessions
        pause and assistant calls are refused until midnight UTC.
      </p>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {FIELDS.map((f) => (
          <label key={f.key} className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">{f.label}</span>
            <input
              type="number"
              min={0}
              value={draft[f.key]}
              onChange={(e) =>
                setDraft({ ...draft, [f.key]: Math.max(0, Number(e.target.value) || 0) })
              }
              className="rounded-md border border-border bg-background px-2 py-1"
            />
          </label>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate(draft)}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          Save limits
        </button>
        {save.error !== null && (
          <span className="text-sm text-destructive">
            Could not save — only organization owners can set limits.
          </span>
        )}
        {save.isSuccess && !dirty && (
          <span className="text-sm text-muted-foreground">Saved.</span>
        )}
      </div>
    </div>
  );
}
