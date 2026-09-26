import { useLabel } from "@/lib/vocabulary/useLabel";

export interface StatusChipRowProps {
  labelKey: string;
  values: string[];
}

/** the `status_set` tag -- an array-of-enum field (conditions, blockers, a work
 * item's lifecycle tags) renders as a chip row, whatever domain it came from. */
export function StatusChipRow({ labelKey, values }: StatusChipRowProps) {
  const t = useLabel();

  return (
    <div className="flex flex-col gap-1" data-widget="status_chip_row">
      <div className="text-xs text-muted-foreground">{t(labelKey)}</div>
      <div className="flex flex-wrap gap-1">
        {values.length === 0 ? (
          <span className="text-xs text-muted-foreground">—</span>
        ) : (
          values.map((value) => (
            <span
              key={value}
              className="rounded-full border border-border bg-muted px-2 py-0.5 text-xs"
            >
              {value}
            </span>
          ))
        )}
      </div>
    </div>
  );
}
