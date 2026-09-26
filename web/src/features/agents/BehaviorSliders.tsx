import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";

type Axis = {
  key: string;
  label_key: string;
  range_min: number;
  range_max: number;
  default: number;
  stakes: string;
  semantics_md: string;
  has_gate: boolean;
};

/**
 * the user surface: one slider per pack-defined behavior axis. Saving appends a NEW
 * profile version (history is immutable — every past turn's manifest pins the version
 * it ran under). High-stakes axes (gate-bound: disclosure, deception, malice) carry a
 * badge because they change what the disclosure gate lets this persona do, not just
 * its tone.
 */
export function BehaviorSliders({ personaId }: { personaId: string }) {
  const t = useLabel();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Record<string, number> | null>(null);

  const { data } = useQuery({
    queryKey: ["persona-behavior", personaId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/agents/{persona_id}/behavior", {
        params: { path: { persona_id: personaId } },
      });
      if (error) throw error;
      return data;
    },
  });

  useEffect(() => {
    setDraft(null); // a fresh fetch resets unsaved slider positions
  }, [personaId]);

  const save = useMutation({
    mutationFn: async () => {
      if (!data?.pack_id || draft === null) return;
      const { error } = await apiClient.PUT("/agents/{persona_id}/behavior", {
        params: { path: { persona_id: personaId } },
        body: { pack_id: data.pack_id, axis_values: draft },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setDraft(null);
      queryClient.invalidateQueries({ queryKey: ["persona-behavior", personaId] });
    },
  });

  const axes = (data?.axes ?? []) as Axis[];
  if (!data || axes.length === 0) {
    return (
      <fieldset className="rounded-md border border-dashed border-border p-3 opacity-60">
        <legend className="px-1 text-sm font-medium text-muted-foreground">
          {t("entity.behavior_profile")}
        </legend>
        <p className="text-xs text-muted-foreground">
          No behavior axes are loaded for this organization&apos;s workflow pack.
        </p>
      </fieldset>
    );
  }

  const stored = (data.axis_values ?? {}) as Record<string, number>;
  const values = draft ?? stored;
  // The pack-declared resting value (malice 0, cooperativeness 100, ...); the midpoint
  // is only a fallback for an axis whose pack predates the `default` field.
  const resting = (a: Axis) => a.default ?? Math.round((a.range_min + a.range_max) / 2);

  return (
    <fieldset className="rounded-md border border-border p-3">
      <legend className="px-1 text-sm font-medium">
        {t("entity.behavior_profile")}
        {data.version > 0 && (
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            v{data.version}
          </span>
        )}
      </legend>
      <div className="flex flex-col gap-3">
        {axes.map((axis) => {
          const value = values[axis.key] ?? resting(axis);
          return (
            <div key={axis.key}>
              <div className="flex items-center justify-between text-sm">
                <span>
                  {t(axis.label_key) || axis.key}
                  {axis.stakes === "high" && (
                    <span
                      className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-400"
                      title="Gate-enforced: this dial changes what the disclosure gate permits, not just tone."
                    >
                      gate
                    </span>
                  )}
                </span>
                <span className="font-mono text-xs text-muted-foreground">{value}</span>
              </div>
              <input
                type="range"
                className="w-full"
                min={axis.range_min}
                max={axis.range_max}
                value={value}
                onChange={(e) =>
                  setDraft({ ...values, [axis.key]: Number(e.target.value) })
                }
              />
              <p className="text-xs text-muted-foreground">{axis.semantics_md}</p>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          className="rounded-md border px-3 py-1.5 text-sm disabled:opacity-50"
          disabled={draft === null || save.isPending}
          onClick={() => save.mutate()}
        >
          Save disposition
        </button>
        {save.error !== null && (
          <span className="text-sm text-destructive">Failed to save.</span>
        )}
      </div>
    </fieldset>
  );
}
