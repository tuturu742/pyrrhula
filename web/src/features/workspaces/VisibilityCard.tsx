import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

/**
 * Who-knows-what, made legible. A persona x scope matrix — a check where a persona is
 * entitled to read a scope — plus which knowledge source sits in each scope. The
 * entitlement column is computed by the same `scopes_for` the assembler runs, so this is
 * the actual picture, not a restatement of it. Read-only: scopes are seeded by a workflow
 * or authored in a `.pyr`; this is the window, not the editor.
 */
export function VisibilityCard({ workspaceId }: { workspaceId: string }) {
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

  if (!data) return null;
  const { scopes, personas, knowledge } = data;

  const knowledgeByScope = new Map<string, typeof knowledge>();
  for (const k of knowledge) {
    const list = knowledgeByScope.get(k.scope_key) ?? [];
    list.push(k);
    knowledgeByScope.set(k.scope_key, list);
  }

  return (
    <section className="flex flex-col gap-4 rounded-md border border-border p-4">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-medium">Visibility</h2>
        <p className="text-sm text-muted-foreground">
          Which scopes each persona can read. A knowledge source or secret filed under a
          scope reaches only the personas checked in its column — enforced in the database,
          not requested of the model.
        </p>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr>
              <th className="border-b border-border px-2 py-2 text-left font-medium">
                Persona
              </th>
              {scopes.map((s) => (
                <th
                  key={s.key}
                  className="border-b border-border px-2 py-2 text-center align-bottom"
                >
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
            {personas.map((p) => (
              <tr key={p.id}>
                <td className="border-b border-border/60 px-2 py-2">
                  <div className="font-medium">{p.name}</div>
                  <div className="text-[11px] text-muted-foreground">{p.persona_type}</div>
                </td>
                {scopes.map((s) => {
                  const has = p.scopes.includes(s.key);
                  return (
                    <td
                      key={s.key}
                      className="border-b border-border/60 px-2 py-2 text-center"
                    >
                      {has ? (
                        <span
                          className="text-emerald-600 dark:text-emerald-400"
                          title={`${p.name} can read ${s.key}`}
                          aria-label="entitled"
                        >
                          ✓
                        </span>
                      ) : (
                        <span
                          className="text-muted-foreground/30"
                          title={`${p.name} cannot read ${s.key}`}
                          aria-label="not entitled"
                        >
                          ·
                        </span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

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
                  <span>
                    {(knowledgeByScope.get(s.key) ?? [])
                      .map((k) => `${k.source_name} (${k.class})`)
                      .join(", ")}
                  </span>
                </li>
              ))}
          </ul>
        </div>
      )}
    </section>
  );
}
