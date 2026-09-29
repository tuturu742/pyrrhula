import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";

/**
 * Is there room to retrieve anything?
 *
 * Each phase of a flow gives every knowledge class a slice of its token budget, and each
 * slice takes the always-on (`constant`) entries first, up to a share of it, before
 * search gets the rest. Two things the author cannot otherwise see: a class where even
 * that share leaves no usable room, and always-on entries that do not fit the share and
 * so appear only when search leaves room for them.
 *
 * Measured on the sample that prompted this card, before the share existed: nine constant
 * rules entries against a resolve phase that budgets rules ~1,470, eight lore entries
 * against 630, and a 700-chunk rulebook attached beside them that reached zero of thirty
 * turns.
 */
export function KnowledgeBudgetCard({ workspaceId }: { workspaceId: string }) {
  const t = useLabel();
  const [flowId, setFlowId] = useState<string>("");

  const flows = useQuery({
    queryKey: ["knowledge-budget-flows", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions", {
        params: { query: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const selected = flowId || flows.data?.[0]?.id || "";
  const saturation = useQuery({
    queryKey: ["knowledge-saturation", workspaceId, selected],
    enabled: selected !== "",
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/workspaces/{workspace_id}/saturation", {
        params: {
          path: { workspace_id: workspaceId },
          query: { process_definition_id: selected },
        },
      });
      if (error) throw error;
      return data;
    },
  });

  if (flows.isSuccess && (flows.data?.length ?? 0) === 0) return null;

  const phases = saturation.data?.phases ?? [];
  const problems = phases.flatMap((phase) =>
    phase.classes
      .filter((c) => c.saturated || c.tight || c.dropped_constant_entries > 0)
      .map((c) => ({ phase: phase.phase_key, ...c })),
  );

  return (
    <section className="flex flex-col gap-4 rounded-md border border-border p-4">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-medium">Knowledge budget</h2>
        <p className="text-sm text-muted-foreground">
          What each phase of a flow can retrieve, per class. Always-on entries are placed
          first, up to a share of the slice, and search spends the rest. Always-on entries
          past that share appear only when search leaves room for them.
        </p>
      </div>

      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">Flow</span>
        <select
          className="w-full max-w-sm rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={selected}
          onChange={(e) => setFlowId(e.target.value)}
        >
          {(flows.data ?? []).map((f) => (
            <option key={f.id} value={f.id}>
              {f.name}
            </option>
          ))}
        </select>
      </label>

      {saturation.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {problems.length > 0 && (
        <div className="flex flex-col gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
          {problems.map((p) => (
            <p key={`${p.phase}-${p.class}`}>
              <b>
                {t(`phase.${p.phase}`)} · {t(`class.${p.class}`)}
              </b>{" "}
              {p.saturated ? (
                <>
                  — one always-on entry occupies the whole {p.bucket_tokens}-token share, so
                  nothing else can be retrieved in this phase.
                </>
              ) : p.dropped_constant_entries > 0 ? (
                <>
                  — {p.admitted_constant_entries} of {p.constant_entries} always-on entries fit
                  the {p.bucket_tokens}-token share. The rest appear only when search leaves
                  room, so treat them as optional here or shorten them.
                </>
              ) : (
                <>
                  — {p.retrievable_tokens} tokens left for search after{" "}
                  {p.admitted_constant_entries} always-on{" "}
                  {p.admitted_constant_entries === 1 ? "entry" : "entries"}, which is under one
                  average chunk.
                </>
              )}
              {p.dropped_constant_entries > 0 && (
                <span className="text-muted-foreground">
                  {" "}
                  Offered in your order:{" "}
                  {p.constant_entry_keys
                    .slice(0, p.admitted_constant_entries)
                    .join(", ")}
                  {p.admitted_constant_entries > 0 ? ", then " : ""}
                  {p.constant_entry_keys
                    .slice(p.admitted_constant_entries, p.admitted_constant_entries + 4)
                    .join(", ")}
                  {p.constant_entries > p.admitted_constant_entries + 4 ? ", …" : ""}.
                </span>
              )}
            </p>
          ))}
        </div>
      )}

      {saturation.isSuccess && problems.length === 0 && (
        <p className="text-xs text-muted-foreground">
          Every class this flow budgets has room left for retrieval.
        </p>
      )}

      {phases.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="py-1 pr-3 font-normal">Phase</th>
                <th className="py-1 pr-3 font-normal">Class</th>
                <th className="py-1 pr-3 text-right font-normal">Share</th>
                <th className="py-1 pr-3 text-right font-normal">Always-on</th>
                <th className="py-1 pr-3 text-right font-normal">Fits</th>
                <th className="py-1 text-right font-normal">Left to retrieve</th>
              </tr>
            </thead>
            <tbody>
              {phases.flatMap((phase) =>
                phase.classes.map((c) => (
                  <tr key={`${phase.phase_key}-${c.class}`} className="border-b border-border/50">
                    <td className="py-1 pr-3">{t(`phase.${phase.phase_key}`)}</td>
                    <td className="py-1 pr-3">{t(`class.${c.class}`)}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">{c.bucket_tokens}</td>
                    <td className="py-1 pr-3 text-right tabular-nums">
                      {c.constant_tokens || "—"}
                    </td>
                    <td
                      className={`py-1 pr-3 text-right tabular-nums ${
                        c.dropped_constant_entries > 0 ? "text-amber-600" : ""
                      }`}
                    >
                      {c.constant_entries === 0
                        ? "—"
                        : `${c.admitted_constant_entries}/${c.constant_entries}`}
                    </td>
                    <td
                      className={`py-1 text-right tabular-nums ${
                        c.saturated ? "text-destructive" : c.tight ? "text-amber-600" : ""
                      }`}
                    >
                      {c.retrievable_tokens}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
