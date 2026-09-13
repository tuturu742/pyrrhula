import { useQuery } from "@tanstack/react-query";
import { getEntityHistory } from "../api";

export interface HistoryChartProps {
  entityId: string;
  fieldPath: string;
}

/**
 * Per-field timeline from `entity_state_change` (F3.10) -- a numeric field's
 * progression chart and any other numeric field's history are *the same component*,
 * just a different `fieldPath`; there is no separate "progression chart" widget.
 */
export function HistoryChart({ entityId, fieldPath }: HistoryChartProps) {
  const { data: entries } = useQuery({
    queryKey: ["entity-history", entityId, fieldPath],
    queryFn: () => getEntityHistory(entityId, fieldPath),
  });

  if (!entries || entries.length === 0) return null;

  const numericValues = entries
    .map((e) => (typeof e.new_value === "number" ? e.new_value : null))
    .filter((v): v is number => v !== null);
  const isNumeric = numericValues.length === entries.length;
  const min = isNumeric ? Math.min(...numericValues) : 0;
  const max = isNumeric ? Math.max(...numericValues) : 0;
  const span = max - min || 1;

  return (
    <div className="mt-1 flex items-end gap-0.5" data-widget="history_chart" title={fieldPath}>
      {isNumeric
        ? numericValues.map((value, i) => (
            <div
              key={i}
              className="w-1.5 rounded-t bg-muted-foreground/40"
              style={{ height: `${4 + ((value - min) / span) * 16}px` }}
              title={String(value)}
            />
          ))
        : entries.map((entry) => (
            <span key={entry.id} className="text-[10px] text-muted-foreground">
              {String(entry.new_value)}
            </span>
          ))}
    </div>
  );
}
