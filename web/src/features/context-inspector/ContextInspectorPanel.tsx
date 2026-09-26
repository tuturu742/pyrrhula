import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

interface ContextInspectorPanelProps {
  messageId: string;
}

/**
 * per-message transparency (-- "a differentiator, not a debug tool").
 * Everything here reads straight from the durably-written ContextManifest (INV-10) and
 * usage_record -- nothing is recomputed or guessed at render time. Permission-checked
 * server-side by its own get_manifest_for_message (a 403 renders as a plain "not
 * visible to you" message, not a silent empty panel).
 */
export function ContextInspectorPanel({ messageId }: ContextInspectorPanelProps) {
  const manifestQuery = useQuery({
    queryKey: ["message-manifest", messageId],
    queryFn: async () => {
      const { data, error, response } = await apiClient.GET("/messages/{message_id}/manifest", {
        params: { path: { message_id: messageId } },
      });
      if (error) {
        if (response.status === 403 || response.status === 404) return null;
        throw error;
      }
      return data;
    },
  });

  const citationsQuery = useQuery({
    queryKey: ["message-citations", messageId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/messages/{message_id}/citations", {
        params: { path: { message_id: messageId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const usageQuery = useQuery({
    queryKey: ["message-usage", messageId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/messages/{message_id}/usage", {
        params: { path: { message_id: messageId } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (manifestQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading context…</p>;
  }
  if (manifestQuery.data === null) {
    return (
      <p className="text-sm text-muted-foreground">
        No manifest visible to you for this message (either none was recorded, or you
        weren't the viewer and don't hold a role that can see any viewer's manifest).
      </p>
    );
  }
  const manifest = manifestQuery.data;
  if (!manifest) return null;

  const bucketTotals = new Map<string, number>();
  for (const entry of manifest.entries) {
    bucketTotals.set(entry.bucket, (bucketTotals.get(entry.bucket) ?? 0) + entry.token_count);
  }
  const grandTotal = [...bucketTotals.values()].reduce((a, b) => a + b, 0);

  const entriesByClass = new Map<string, typeof manifest.entries>();
  for (const entry of manifest.entries) {
    const list = entriesByClass.get(entry.class) ?? [];
    list.push(entry);
    entriesByClass.set(entry.class, list);
  }

  return (
    <div className="flex flex-col gap-4 rounded-md border border-border p-4 text-sm">
      <section className="flex flex-col gap-2">
        <h3 className="font-medium">Budget composition</h3>
        {grandTotal === 0 ? (
          <p className="text-xs text-muted-foreground">No knowledge content in this manifest.</p>
        ) : (
          <div className="flex h-4 w-full overflow-hidden rounded-full border border-border">
            {[...bucketTotals.entries()].map(([bucket, tokens], i) => (
              <div
                key={bucket}
                title={`${bucket}: ${tokens} tokens (${((tokens / grandTotal) * 100).toFixed(1)}%)`}
                className={i % 2 === 0 ? "bg-primary" : "bg-secondary-foreground/60"}
                style={{ width: `${(tokens / grandTotal) * 100}%` }}
              />
            ))}
          </div>
        )}
        <ul className="flex flex-wrap gap-3 text-xs text-muted-foreground">
          {[...bucketTotals.entries()].map(([bucket, tokens]) => (
            <li key={bucket}>
              {bucket}: {tokens} tok ({grandTotal ? ((tokens / grandTotal) * 100).toFixed(0) : 0}%)
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="font-medium">Entries</h3>
        {manifest.entries.length === 0 && (
          <p className="text-xs text-muted-foreground">No knowledge entries were included.</p>
        )}
        {[...entriesByClass.entries()].map(([className, entries]) => (
          <div key={className} className="flex flex-col gap-1">
            <div className="text-xs font-medium uppercase text-muted-foreground">{className}</div>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="pr-2">Entry</th>
                  <th className="pr-2">Bucket</th>
                  <th className="pr-2">Rank</th>
                  <th className="pr-2">Score</th>
                  <th className="pr-2">Why</th>
                  <th className="pr-2">Tokens</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.citation_id} className="border-t border-border">
                    <td className="pr-2 py-1">
                      {e.entry_key}
                      {e.version_id && (
                        <span className="text-muted-foreground"> (v{e.version_id.slice(0, 8)})</span>
                      )}
                    </td>
                    <td className="pr-2">{e.bucket}</td>
                    <td className="pr-2">{e.rank}</td>
                    <td className="pr-2">{e.score.toFixed(3)}</td>
                    <td className="pr-2">{e.why}</td>
                    <td className="pr-2">{e.token_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="font-medium">Citations</h3>
        {citationsQuery.data && citationsQuery.data.valid.length === 0 && (
          <p className="text-xs text-muted-foreground">No citations in this reply.</p>
        )}
        <ul className="flex flex-col gap-1">
          {citationsQuery.data?.valid.map((c) => (
            <li key={c.citation_id} className="rounded-md border border-border p-2 text-xs">
              <span className="font-mono text-muted-foreground">[{c.citation_id}]</span>{" "}
              <span className="font-medium">{c.title}</span>
              <div className="mt-1 text-muted-foreground">{c.body_md}</div>
            </li>
          ))}
        </ul>
        {citationsQuery.data && citationsQuery.data.bad_citation_ids.length > 0 && (
          <ul className="flex flex-wrap gap-1">
            {citationsQuery.data.bad_citation_ids.map((id) => (
              <li
                key={id}
                className="rounded-full bg-destructive px-2 py-0.5 text-xs text-destructive-foreground"
              >
                hallucinated: [{id}]
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-1">
        <h3 className="font-medium">Redactions</h3>
        {manifest.redactions.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            None for this turn.
          </p>
        ) : (
          <ul className="flex flex-col gap-1">
            {manifest.redactions.map((r, i) => (
              <li key={i} className="text-xs text-muted-foreground">
                {r.type} {r.id}: {r.reason}
              </li>
            ))}
          </ul>
        )}
      </section>

      {usageQuery.data && (
        <section className="flex flex-col gap-1">
          <h3 className="font-medium">Token spend</h3>
          <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
            <span>prompt: {usageQuery.data.prompt_tokens}</span>
            <span>completion: {usageQuery.data.completion_tokens}</span>
            <span>cached: {usageQuery.data.cached_tokens}</span>
            <span>cache hit: {(usageQuery.data.cache_hit_rate * 100).toFixed(0)}%</span>
            <span>est. cost: ${usageQuery.data.estimated_cost}</span>
          </div>
        </section>
      )}
    </div>
  );
}
