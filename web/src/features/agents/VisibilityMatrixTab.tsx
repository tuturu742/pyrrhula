import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

/**
 * The persona x scope matrix, editable. Group-scope cells are toggles — clicking one
 * grants or revokes that persona's membership; the change is written by
 * PUT /workspaces/{id}/personas/{persona_id}/scopes and re-read through the same
 * `scopes_for` the assembler uses, so the grid always shows the real entitlement.
 * Public/role cells are read-only (not per-persona switches). The assistant is shown but
 * not editable: its grounding is re-scoped to whoever is asking, so it holds no grants.
 */
export function VisibilityMatrixTab({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
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

  const setScopes = useMutation({
    mutationFn: async ({ personaId, scopes }: { personaId: string; scopes: string[] }) => {
      const { error } = await apiClient.PUT(
        "/workspaces/{workspace_id}/personas/{persona_id}/scopes",
        {
          params: { path: { workspace_id: workspaceId, persona_id: personaId } },
          body: { scopes },
        },
      );
      if (error) throw new Error(detailOf(error));
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-visibility", workspaceId] });
    },
  });

  if (!data) return null;
  const { scopes, personas, knowledge } = data;
  const groupKeys = new Set(scopes.filter((s) => s.kind === "group").map((s) => s.key));

  const toggle = (persona: (typeof personas)[number], key: string) => {
    const groupMembership = persona.scopes.filter((k) => groupKeys.has(k));
    const next = groupMembership.includes(key)
      ? groupMembership.filter((k) => k !== key)
      : [...groupMembership, key];
    setScopes.mutate({ personaId: persona.id, scopes: next });
  };

  const knowledgeByScope = new Map<string, string[]>();
  for (const k of knowledge) {
    const list = knowledgeByScope.get(k.scope_key) ?? [];
    list.push(`${k.source_name} (${k.class})`);
    knowledgeByScope.set(k.scope_key, list);
  }

  return (
    <section className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Which scopes each persona can read. Toggle a{" "}
        <span className="font-mono text-xs">group</span> cell to grant or revoke access;
        knowledge and secrets filed under that scope then reach exactly the checked
        personas — enforced in the database, not asked of the model. Public and role scopes
        are not per-persona switches.
      </p>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              <th className="border-b border-border px-2 py-2 text-left font-medium">Persona</th>
              {scopes.map((s) => (
                <th key={s.key} className="border-b border-border px-2 py-2 text-center align-bottom">
                  <div className="flex flex-col items-center gap-0.5">
                    <span className="font-mono text-xs">{s.key}</span>
                    <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                      {s.kind}
                      {s.member_count !== null ? ` · ${s.member_count}` : ""}
                    </span>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {personas.map((p) => {
              const assistant = p.persona_type === "informational";
              return (
                <tr key={p.id}>
                  <td className="border-b border-border/60 px-2 py-2">
                    <div className="font-medium">{p.name}</div>
                    <div className="text-[11px] text-muted-foreground">{p.persona_type}</div>
                  </td>
                  {scopes.map((s) => {
                    const has = p.scopes.includes(s.key);
                    const editable = s.kind === "group" && !assistant;
                    return (
                      <td key={s.key} className="border-b border-border/60 px-2 py-2 text-center">
                        {editable ? (
                          <input
                            type="checkbox"
                            checked={has}
                            disabled={setScopes.isPending}
                            onChange={() => toggle(p, s.key)}
                            title={`${has ? "Revoke" : "Grant"} ${p.name} → ${s.key}`}
                          />
                        ) : has ? (
                          <span
                            className="text-emerald-600 dark:text-emerald-400"
                            title={
                              assistant
                                ? "The assistant reads as whoever is asking"
                                : `${s.key} is not a per-persona switch`
                            }
                          >
                            ✓
                          </span>
                        ) : (
                          <span className="text-muted-foreground/30">·</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {setScopes.error !== null && (
        <p className="text-sm text-destructive">
          {setScopes.error instanceof Error
            ? setScopes.error.message
            : "Failed to update visibility."}
        </p>
      )}

      <p className="text-[11px] text-muted-foreground">
        The assistant row is not editable: it grounds answers in the requesting person's own
        entitlements, so it needs no standing grants.
      </p>

      {knowledge.length > 0 && (
        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-medium text-muted-foreground">Knowledge by scope</h3>
          <ul className="flex flex-col gap-1 text-sm">
            {scopes
              .filter((s) => knowledgeByScope.has(s.key))
              .map((s) => (
                <li key={s.key} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-mono text-xs text-muted-foreground">{s.key}</span>
                  <span className="text-muted-foreground">—</span>
                  <span>{(knowledgeByScope.get(s.key) ?? []).join(", ")}</span>
                </li>
              ))}
          </ul>
        </div>
      )}
    </section>
  );
}


/** Pull the API's `detail` (string or FastAPI validation array) out of an openapi-fetch
 * error, so a 403 shows "manage_workspace required" instead of a blank failure. */
function detailOf(error: unknown): string {
  const d = (error as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d[0] && typeof d[0] === "object" && "msg" in d[0])
    return String((d[0] as { msg: unknown }).msg);
  return "Failed to update visibility. You may not have permission to manage this workspace.";
}
