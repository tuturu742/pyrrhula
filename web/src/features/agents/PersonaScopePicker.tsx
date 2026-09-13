import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

/**
 * Which knowledge/secret scopes this persona can read, as a multiselect of the
 * workspace's GROUP scopes. Public scopes (everyone) and role scopes (by persona type)
 * are shown for context but not toggled here — they are not per-persona switches. The
 * informational assistant gets a note instead: its grounding is re-scoped to whoever is
 * asking, so it needs no standing grants.
 */
export function PersonaScopePicker({
  workspaceId,
  personaId,
  personaType,
}: {
  workspaceId: string;
  personaId: string;
  personaType: string;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Set<string> | null>(null);

  const { data } = useQuery({
    queryKey: ["workspace-visibility", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/visibility", {
        params: { path: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const me = data?.personas.find((p) => p.id === personaId);
  useEffect(() => setDraft(null), [personaId]);

  const save = useMutation({
    mutationFn: async (scopes: string[]) => {
      const { error } = await apiClient.PUT("/workspaces/{workspace_id}/personas/{persona_id}/scopes", {
        params: { path: { workspace_id: workspaceId, persona_id: personaId } },
        body: { scopes },
      });
      if (error) throw new Error(detailOf(error));
    },
    onSuccess: () => {
      setDraft(null);
      queryClient.invalidateQueries({ queryKey: ["workspace-visibility", workspaceId] });
    },
  });

  if (personaType === "informational") {
    return (
      <fieldset className="rounded-md border border-dashed border-border p-3 opacity-70">
        <legend className="px-1 text-sm font-medium text-muted-foreground">Visibility</legend>
        <p className="text-xs text-muted-foreground">
          The assistant reads knowledge as <em>whoever is asking</em> — its answers are
          re-scoped to the requesting person's own entitlements, so it can never surface
          something that person could not read directly. There is nothing to grant here.
        </p>
      </fieldset>
    );
  }

  if (!data || !me) return null;

  const groupScopes = data.scopes.filter((s) => s.kind === "group");
  const alwaysOn = data.scopes.filter((s) => s.kind !== "group");
  const current = draft ?? new Set(me.scopes);

  const toggle = (key: string) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setDraft(next);
  };

  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-border p-3">
      <legend className="px-1 text-sm font-medium">Visibility (scopes)</legend>
      {groupScopes.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No group scopes in this workspace yet. Knowledge filed under a group scope would
          appear here as a toggle.
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {groupScopes.map((s) => (
            <label key={s.key} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={current.has(s.key)}
                onChange={() => toggle(s.key)}
              />
              <span className="font-mono text-xs">{s.key}</span>
              <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                group{s.member_count !== null ? ` · ${s.member_count}` : ""}
              </span>
            </label>
          ))}
        </div>
      )}
      {alwaysOn.length > 0 && (
        <p className="text-[11px] text-muted-foreground">
          Always readable:{" "}
          {alwaysOn.map((s) => `${s.key} (${s.kind})`).join(", ")}
        </p>
      )}
      {groupScopes.length > 0 && (
        <div className="mt-1 flex items-center gap-2">
          <button
            type="button"
            className="rounded-md border px-3 py-1.5 text-sm disabled:opacity-50"
            disabled={draft === null || save.isPending}
            onClick={() => save.mutate([...current])}
          >
            Save visibility
          </button>
          {save.error !== null && (
            <span className="text-sm text-destructive">
              {save.error instanceof Error ? save.error.message : "Failed to save."}
            </span>
          )}
        </div>
      )}
    </fieldset>
  );
}


function detailOf(error: unknown): string {
  const d = (error as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  return "Failed to save. You may not have permission to manage this workspace.";
}
