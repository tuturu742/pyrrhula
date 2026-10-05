import { useFieldLabel } from "../fieldLabel";

export interface ProgressionMeterProps {
  labelKey: string;
  value: number;
  /** Optional server-provided "value needed for the next threshold" -- when absent,
   * renders the raw value only (no bar), since without a curve reference there's
   * nothing to show progress *toward*. */
  nextThreshold: number | null;
}

/** the `progression` tag -- XP-to-next-level and a work item's story-point rollup
 * are the same meter, differing only in `curve_ref`/metadata values. */
export function ProgressionMeter({ labelKey, value, nextThreshold }: ProgressionMeterProps) {
  const t = useFieldLabel();
  const pct =
    nextThreshold !== null && nextThreshold > 0
      ? Math.max(0, Math.min(100, (value / nextThreshold) * 100))
      : null;

  return (
    <div className="flex flex-col gap-1" data-widget="progression_meter">
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{t(labelKey)}</span>
        <span>
          {value}
          {nextThreshold !== null ? ` / ${nextThreshold}` : ""}
        </span>
      </div>
      {pct !== null && (
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}
