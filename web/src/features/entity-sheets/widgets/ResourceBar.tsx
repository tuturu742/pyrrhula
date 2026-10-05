import { useFieldLabel } from "../fieldLabel";

export interface ResourceBarProps {
  labelKey: string;
  value: number;
  max: number | null;
  lowThreshold: number | null;
}

/** the `resource` tag -> this one component, for an HP bar, a budget bar, or a
 * work item's remaining estimate alike -- the tag, not the field name, decides this
 * renders here. */
export function ResourceBar({ labelKey, value, max, lowThreshold }: ResourceBarProps) {
  const t = useFieldLabel();
  const pct = max && max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : null;
  const low = lowThreshold !== null && value <= lowThreshold;

  return (
    <div className="flex flex-col gap-1" data-widget="resource_bar">
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{t(labelKey)}</span>
        <span>
          {value}
          {max !== null ? ` / ${max}` : ""}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={`h-full rounded-full transition-all ${low ? "bg-destructive" : "bg-primary"}`}
          style={{ width: pct !== null ? `${pct}%` : "100%" }}
        />
      </div>
    </div>
  );
}
