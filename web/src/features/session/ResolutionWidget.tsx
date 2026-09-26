import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

interface ResolutionWidgetProps {
  messageId: string;
}

/**
 * INV-7: renders straight from `ResolutionRecord` (via `GET
 * /messages/{id}/resolutions`) -- expression, rolls, modifier, total vs target, outcome.
 * Never parses the message's own prose; a mismatch between what this widget shows and
 * what the narration claims is exactly what the `contradicted` badge is for, not a
 * reason to trust the narration instead.
 */
export function ResolutionWidget({ messageId }: ResolutionWidgetProps) {
  const { data: resolutions } = useQuery({
    queryKey: ["message-resolutions", messageId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/messages/{message_id}/resolutions", {
        params: { path: { message_id: messageId } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (!resolutions || resolutions.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5">
      {resolutions.map((record) => (
        <div
          key={record.id}
          className="flex flex-col gap-1 rounded-md border border-border border-l-2 border-l-primary bg-muted/40 px-3 py-2 text-sm"
        >
          <div className="flex items-center gap-2">
            <span
              title="Resolved by the engine, not the model: a seeded, code-executed roll checked against the character sheet and recorded. The narration cannot change it."
              className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-primary"
            >
              🎲 Engine roll
            </span>
            <span className="font-mono text-[11px] text-muted-foreground">{record.tool_key}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-muted-foreground">{record.expression}</span>
            <span className="font-mono text-xs text-muted-foreground">
              [{record.rolls.join(", ")}]
              {typeof record.modifiers.total === "number" && record.modifiers.total !== 0
                ? ` ${record.modifiers.total >= 0 ? "+" : ""}${record.modifiers.total}`
                : ""}
            </span>
            {record.contradicted && (
              <span
                title="The narration didn't match this record"
                className="rounded-full bg-destructive px-2 py-0.5 text-xs font-medium text-destructive-foreground"
              >
                correction
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 font-medium">
            <span>Total: {record.total}</span>
            {record.target !== null && <span className="text-muted-foreground">vs {record.target}</span>}
            <span
              className={
                record.outcome === "success" ? "text-emerald-500" : "text-destructive"
              }
            >
              {record.outcome.toUpperCase()}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
